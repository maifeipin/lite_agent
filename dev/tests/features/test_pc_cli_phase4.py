"""
PC CLI Phase 4 单元测试 (tests/test_pc_cli_phase4.py).
覆盖：
1. conf.d/llm.json 脱敏模型目录快照与请求级模型切换（拒绝未配置模型，不泄露 API Key）；
2. LLM 总管三权分立工具集：查进度、创建 Bot、强制发起授权删除/中止（绝不直接物理删除）；
3. 总体统计看板聚合 (nodes / bots / turns / manager_usage / pending_approvals)；
4. 宿主 PluginContext 配置投影与 Phase 4 路由注册。
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List

from lite_agent.app.bootstrap import create_app
from lite_agent.features.pc_cli.contracts import ApprovalType, TurnStatus
from lite_agent.features.pc_cli.hub import PcCliHub
from lite_agent.features.pc_cli.manager import PcCliManager
from lite_agent.features.pc_cli.repository import PcCliRepository
from lite_agent.features.pc_cli.stats import collect_dashboard_stats


_SAMPLE_LLM_CONFIG = {
    "llm": {
        "default": "flash",
        "models": {
            "flash": {
                "driver": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-secret-do-not-leak",
                "model": "deepseek-v4-flash",
                "tags": ["fast"],
            },
            "gemini-pro": {
                "driver": "gemini_native",
                "api_key": "gm-secret-do-not-leak",
                "model": "gemini-2.5-pro",
                "tags": ["reasoning"],
            },
        },
    }
}


class _ScriptedGateway:
    """按轮次返回预编排工具调用与回复的假 LLMGateway。"""

    def __init__(self, responses: List[Dict[str, Any]]):
        self._responses = list(responses)
        self.called_models: List[str] = []

    def invoke_sync(self, messages, tools=None, **kwargs):
        self.called_models.append(str(kwargs.get("model") or ""))
        assert self._responses, "scripted responses exhausted"
        return self._responses.pop(0)


def test_manager_models_snapshot_redacted_and_validation():
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        gw = _ScriptedGateway([{"content": "ok", "usage_total": 42}])
        mgr = PcCliManager(hub=hub, config=_SAMPLE_LLM_CONFIG, gateway=gw)

        snap = mgr.list_models()
        names = [m["name"] for m in snap["models"]]
        assert "flash" in names and "gemini-pro" in names
        assert snap["default_model"] == "flash"
        # 绝不泄露 API Key 明文
        serialized = json.dumps(snap, ensure_ascii=False)
        assert "sk-secret-do-not-leak" not in serialized
        assert "gm-secret-do-not-leak" not in serialized

        # 未在 llm.json 配置的模型必须拒绝
        ok_bad, res_bad = mgr.chat("hi", model_name="unknown-model-xyz")
        assert ok_bad is False and "不在 conf.d/llm.json" in res_bad["error"]

        # 请求级切换至 gemini-pro
        ok_good, res_good = mgr.chat("hi", model_name="gemini-pro")
        assert ok_good is True
        assert res_good["model"] == "gemini-pro"
        assert gw.called_models == ["gemini-pro"]
    finally:
        hub.close()


def test_manager_create_bot_and_authorized_delete_flow():
    """核心安全测试：LLM 总管可创建 Bot，但删除 Bot 必须发起授权单，确认前 Bot 完好，确认后原子删除。"""
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        hub.handle_daemon_hello("c1", lambda _: None, {"node_id": "mac1", "engines": ["codex", "agy"], "workspace_roots": ["/w"]})

        # 第 1 轮：LLM 调 create_bot；第 2 轮：回复创建完成
        gw_create = _ScriptedGateway([
            {
                "content": "",
                "usage_total": 100,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "create_bot",
                            "arguments": json.dumps({
                                "name": "mac-codex",
                                "node_id": "mac1",
                                "engine": "codex",
                                "cwd": "/w/proj",
                            }),
                        },
                    }
                ],
            },
            {"content": "已在 mac1 上创建 Bot mac-codex。", "usage_total": 80},
        ])
        mgr = PcCliManager(hub=hub, config=_SAMPLE_LLM_CONFIG, gateway=gw_create)
        ok1, res1 = mgr.chat("在 mac1 建一个 codex bot")
        assert ok1 is True
        bots = hub.list_bots()
        assert len(bots) == 1
        bot_id = bots[0]["bot_id"]

        # 第 2 次对话：用户要求删除该 Bot -> LLM 调用 request_delete_bot（严禁直接删除！）
        gw_del = _ScriptedGateway([
            {
                "content": "",
                "usage_total": 90,
                "tool_calls": [
                    {
                        "id": "call_del",
                        "function": {
                            "name": "request_delete_bot",
                            "arguments": json.dumps({"bot_id": bot_id, "reason": "临时测试完毕"}),
                        },
                    }
                ],
            },
            {"content": "已发起删除授权单，请在手机上确认。", "usage_total": 60},
        ])
        mgr.gateway = gw_del
        ok2, res2 = mgr.chat("删掉 mac-codex")
        assert ok2 is True
        assert len(res2["created_approvals"]) == 1
        appr = res2["created_approvals"][0]
        assert appr["approval_type"] == ApprovalType.BOT_DELETE.value
        assert appr["requested_by"] == "llm_manager"

        # 用户未确认前，Bot 必须完好存在！
        assert hub.repo.get_bot(bot_id) is not None

        # 用户在手机点击确认后，原子执行删除
        ok_approve, approve_res = hub.resolve_approval(appr["approval_id"], "allow_once")
        assert ok_approve is True and approve_res["executed"] is True
        assert hub.repo.get_bot(bot_id) is None

        # 验证统计看板准确汇总总管调用与消耗
        stats = collect_dashboard_stats(hub, days=7)
        assert stats["manager_usage"]["calls"] == 2
        assert stats["manager_usage"]["total_tokens"] == 330
    finally:
        hub.close()


def test_dashboard_stats_aggregation():
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        hub.handle_daemon_hello("c1", lambda _: None, {"node_id": "mac1", "engines": ["codex"], "workspace_roots": ["/w"]})
        repo.upsert_node("win1", "Win", "win32", ["agy"], ["C:\\w"], status="offline")

        ok_b, b_res = hub.create_bot(name="b1", node_id="mac1", engine="codex", cwd="/w")
        assert ok_b
        bot_id = b_res["bot"]["bot_id"]

        ok_t1, t1 = hub.start_turn(bot_id, "task 1")
        assert ok_t1
        hub.handle_daemon_turn_finished(
            {"bot_id": bot_id, "turn_id": t1["turn"]["turn_id"], "status": TurnStatus.COMPLETED.value},
            sender_node_id="mac1",
            conn_id="c1",
        )

        ok_t2, t2 = hub.start_turn(bot_id, "task 2")
        assert ok_t2
        hub.handle_daemon_turn_finished(
            {"bot_id": bot_id, "turn_id": t2["turn"]["turn_id"], "status": TurnStatus.FAILED.value, "error": "boom"},
            sender_node_id="mac1",
            conn_id="c1",
        )

        stats = collect_dashboard_stats(hub, days=7)
        assert stats["nodes"] == {"total": 2, "online": 1, "offline": 1}
        assert stats["bots"]["active"] == 1
        assert stats["bots"]["error"] == 1
        assert stats["turns"]["total"] == 2
        assert stats["turns"]["completed"] == 1
        assert stats["turns"]["failed"] == 1
        assert stats["turns"]["success_rate"] == 50.0
        assert stats["turns"]["by_engine"]["codex"] == 2
    finally:
        hub.close()


def test_phase4_routes_registered_and_llm_projected():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = {
            **_SAMPLE_LLM_CONFIG,
            "pc_cli": {
                "enabled": True,
                "db_path": os.path.join(tmp, "pc_cli.db"),
                "start_ws_server": False,
            },
        }
        app = create_app(mode="full", config=cfg, agent=object())
        try:
            app.plugin_loader.load_plugin_by_entry("lite_agent.features.pc_cli", plugin_name="pc_cli")
            assert app.route_registry.match("GET", "/api/v1/pc-cli/models") is not None
            assert app.route_registry.match("GET", "/api/v1/pc-cli/stats") is not None
            assert app.route_registry.match("POST", "/api/v1/pc-cli/manager/chat") is not None
        finally:
            app.shutdown()


def test_turn_cancel_approval_bound_to_original_turn_id():
    """复核点 1：批准旧回合的中止审批单，绝不能误中止同一 Bot 后续新启动的回合。"""
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        hub.handle_daemon_hello("c1", lambda _: None, {"node_id": "mac1", "engines": ["codex"], "workspace_roots": ["/w"]})
        ok_b, b_res = hub.create_bot(name="b1", node_id="mac1", engine="codex", cwd="/w")
        assert ok_b
        bot_id = b_res["bot"]["bot_id"]

        ok_t1, t1_res = hub.start_turn(bot_id, "turn 1")
        assert ok_t1
        t1_id = t1_res["turn"]["turn_id"]

        # 为 T1 创建中止授权单
        appr = repo.create_approval(
            bot_id=bot_id,
            turn_id=t1_id,
            tool_name="cancel_turn",
            summary="中止 T1",
            approval_type=ApprovalType.TURN_CANCEL.value,
            requested_by="llm_manager",
        )

        # T1 正常结束后，自动失效 T1 挂起的中止审批单；随后启动 T2
        hub.handle_daemon_turn_finished(
            {"bot_id": bot_id, "turn_id": t1_id, "status": TurnStatus.COMPLETED.value},
            sender_node_id="mac1",
            conn_id="c1",
        )
        assert repo.get_approval(appr["approval_id"])["decision"] == "expired"

        ok_t2, t2_res = hub.start_turn(bot_id, "turn 2")
        assert ok_t2
        t2_id = t2_res["turn"]["turn_id"]

        # 即使手动构造一条指向旧 T1 的 pending 审批单，批准时也必须被拒且 T2 保持运行
        stale_appr = repo.create_approval(
            bot_id=bot_id,
            turn_id=t1_id,
            tool_name="cancel_turn",
            summary="旧 T1 审批",
            approval_type=ApprovalType.TURN_CANCEL.value,
            requested_by="llm_manager",
        )
        ok_res, res_data = hub.resolve_approval(stale_appr["approval_id"], "allow_once")
        assert ok_res is False and res_data["executed"] is False
        assert repo.get_turn(t2_id)["status"] == TurnStatus.RUNNING.value
    finally:
        hub.close()


def test_get_bot_progress_reads_latest_beyond_1000_events():
    """复核点 3：超过 1,000 条事件后，get_bot_progress 仍返回真正最新的尾部事件。"""
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        hub.handle_daemon_hello("c1", lambda _: None, {"node_id": "mac1", "engines": ["codex"], "workspace_roots": ["/w"]})
        ok_b, b_res = hub.create_bot(name="b_many", node_id="mac1", engine="codex", cwd="/w")
        assert ok_b
        bot_id = b_res["bot"]["bot_id"]

        for i in range(1, 1025):
            repo.append_event(bot_id=bot_id, turn_id="t1", event_type="text_delta", payload={"delta": f"chunk-{i}"})

        mgr = PcCliManager(hub=hub, config=_SAMPLE_LLM_CONFIG, gateway=None)
        res = mgr.execute_tool("get_bot_progress", {"bot_id": bot_id, "limit": 3}, [])
        texts = [e["payload"]["delta"] for e in res["recent_events"]]
        assert texts == ["chunk-1022", "chunk-1023", "chunk-1024"]
    finally:
        hub.close()


def test_create_bot_validates_cwd_roots_and_manager_requires_online():
    """复核点 5：Hub 校验 cwd 白名单根目录；AI 总管拒绝在离线节点建 Bot。"""
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        hub.handle_daemon_hello("c1", lambda _: None, {"node_id": "mac1", "engines": ["codex"], "workspace_roots": ["/Users/dev"]})
        ok_bad, res_bad = hub.create_bot(name="bad", node_id="mac1", engine="codex", cwd="/etc/passwd")
        assert ok_bad is False and "白名单" in res_bad["error"]

        ok_trav, res_trav = hub.create_bot(name="trav", node_id="mac1", engine="codex", cwd="/Users/dev/../../etc")
        assert ok_trav is False and "白名单" in res_trav["error"]

        # 离线节点：AI 总管必须拒绝创建
        repo.upsert_node("off1", "Offline PC", "darwin", ["codex"], ["/Users/dev"], status="offline")
        mgr = PcCliManager(hub=hub, config=_SAMPLE_LLM_CONFIG, gateway=None)
        out = mgr.execute_tool(
            "create_bot",
            {"name": "x", "node_id": "off1", "engine": "codex", "cwd": "/Users/dev/app"},
            [],
        )
        assert out["ok"] is False and "不在线" in out["error"]
    finally:
        hub.close()


def test_daemon_ignores_stale_turn_cancel_for_previous_turn():
    """复核点 1：旧回合迟到的 turn.cancel 绝不得误拒当前新回合的挂起审批或中止新回合。"""
    import asyncio
    from lite_agent.features.pc_cli.daemon.config import DaemonConfig
    from lite_agent.features.pc_cli.daemon.daemon import PcCliDaemon

    class _SpyAdapter:
        def __init__(self):
            self.cancelled = []

        async def cancel_turn(self, bot_id: str, turn_id: str) -> None:
            self.cancelled.append((bot_id, turn_id))

    async def _run():
        spy = _SpyAdapter()
        cfg = DaemonConfig(hub_ws_url="ws://127.0.0.1:1/ws", daemon_token="t", node_id="n1", display_name="n1", workspace_roots=["/"])
        daemon = PcCliDaemon(cfg, adapters={"codex": spy})

        # 模拟当前活跃回合为 t2 且有一个挂起审批 appr_t2
        daemon._active_turns["b1"] = "t2"
        daemon._active_engines["b1"] = "codex"
        fut = asyncio.get_running_loop().create_future()
        daemon._pending_approvals["appr_t2"] = fut
        daemon._turn_approval_ids[("b1", "t2")] = {"appr_t2"}

        # 1) 迟到的旧回合 t1 中止消息必须被忽略，不能影响 t2 的审批与进程
        await daemon._dispatch(None, {"type": "turn.cancel", "bot_id": "b1", "turn_id": "t1"})
        assert not fut.done()
        assert spy.cancelled == []

        # 2) 匹配当前回合 t2 的中止消息才拒绝 t2 审批并中止 t2
        await daemon._dispatch(None, {"type": "turn.cancel", "bot_id": "b1", "turn_id": "t2"})
        assert fut.done() and fut.result() == "deny"
        assert spy.cancelled == [("b1", "t2")]

    asyncio.run(_run())


def test_turn_cancel_approval_distinguishes_requested_vs_forced_vs_confirmed():
    """复核点 2：中止审批严格区分 cancel_requested、hub_marked_cancelled 与 daemon_confirmed。"""
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        hub.handle_daemon_hello("c1", lambda _: None, {"node_id": "mac1", "engines": ["codex"], "workspace_roots": ["/w"]})
        ok_b, b_res = hub.create_bot(name="b1", node_id="mac1", engine="codex", cwd="/w")
        assert ok_b
        bot_id = b_res["bot"]["bot_id"]

        # 1) 在线场景：下发中止请求，但 Daemon 尚未确认停止
        ok_t1, t1 = hub.start_turn(bot_id, "run1")
        assert ok_t1
        a1 = repo.create_approval(
            bot_id=bot_id,
            turn_id=t1["turn"]["turn_id"],
            tool_name="cancel_turn",
            summary="中止 run1",
            approval_type=ApprovalType.TURN_CANCEL.value,
            requested_by="llm_manager",
        )
        ok_r1, r1 = hub.resolve_approval(a1["approval_id"], "allow_once")
        assert ok_r1 is True
        assert r1["cancel_state"] == "cancel_requested"
        assert r1["cancel_requested"] is True
        assert r1["delivered"] is True
        assert r1["daemon_confirmed"] is False
        assert r1["executed"] is False

        # 2) 发送失败/离线场景：Hub 本地强制结束，delivered=False, hub_marked_cancelled=True, daemon_confirmed=False
        hub.handle_daemon_disconnect("mac1", "c1")
        a2 = repo.create_approval(
            bot_id=bot_id,
            turn_id=t1["turn"]["turn_id"],
            tool_name="cancel_turn",
            summary="离线强制中止 run1",
            approval_type=ApprovalType.TURN_CANCEL.value,
            requested_by="llm_manager",
        )
        ok_r2, r2 = hub.resolve_approval(a2["approval_id"], "allow_once")
        assert ok_r2 is True
        assert r2["cancel_state"] == "hub_marked_cancelled"
        assert r2["delivered"] is False
        assert r2["hub_marked_cancelled"] is True
        assert r2["daemon_confirmed"] is False
        assert r2["forced"] is True
    finally:
        hub.close()
