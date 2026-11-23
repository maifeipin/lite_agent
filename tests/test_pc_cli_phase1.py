"""
PC CLI Phase 1 单元与端到端联调测试 (tests/test_pc_cli_phase1.py).
覆盖：
1. 插件默认关闭与显式启用/清理生命周期；
2. SQLite 仓储、单调 seq_id 增量事件与审批原子 CAS；
3. Codex / AGY 适配器命令构建与 JSONL 解析；
4. 真实 loopback WebSocket 端到端闭环（Hub + PcCliWsServer + PcCliDaemon + 手机客户端订阅）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

import pytest

from lite_agent.app.bootstrap import create_app
from lite_agent.features.pc_cli.contracts import ApprovalType, BotStatus, NodeStatus, TurnStatus
from lite_agent.features.pc_cli.daemon._ws_client import StdlibWsClient
from lite_agent.features.pc_cli.daemon.adapters.agy_adapter import AgyCliAdapter, parse_agy_json_line, unwrap_agy_event
from lite_agent.features.pc_cli.daemon.adapters.base import BaseCliAdapter, EventCallback, TurnResult
from lite_agent.features.pc_cli.daemon.adapters.codex_adapter import (
    CodexCliAdapter,
    extract_codex_tool_payload,
    parse_codex_json_line,
)
from lite_agent.features.pc_cli.daemon.config import DaemonConfig
from lite_agent.features.pc_cli.daemon.daemon import PcCliDaemon
from lite_agent.features.pc_cli.daemon.env_probe import build_spawn_argv, is_path_within_roots, list_safe_dirs
from lite_agent.features.pc_cli.hub import PcCliHub
from lite_agent.features.pc_cli.repository import PcCliRepository
from lite_agent.features.pc_cli.ws_server import PcCliWsServer


class _FakeCliAdapter(BaseCliAdapter):
    """用于端到端测试的可控假 CLI 引擎。"""

    def __init__(self, delay: float = 0.02):
        self.delay = delay
        self.cancelled_keys: List[tuple[str, str]] = []
        self._cancel_events: Dict[tuple[str, str], asyncio.Event] = {}

    async def run_turn(
        self,
        *,
        bot_id: str,
        turn_id: str,
        cwd: str,
        prompt: str,
        native_session_id: Optional[str],
        approval_mode: str,
        model: Optional[str],
        on_event: EventCallback,
        on_approval=None,
    ) -> TurnResult:
        key = (bot_id, turn_id)
        cancel_ev = asyncio.Event()
        self._cancel_events[key] = cancel_ev

        await on_event("thinking", {"text": f"分析任务: {prompt}"})
        await on_event("tool_start", {"tool_id": "t1", "tool_name": "command_execution", "command": "pytest -q"})

        if prompt == "WAIT_FOR_CANCEL":
            try:
                await asyncio.wait_for(cancel_ev.wait(), timeout=5.0)
                return TurnResult(status="cancelled", native_session_id=native_session_id or "sess_fake_1")
            except asyncio.TimeoutError:
                return TurnResult(status="failed", error="cancel timeout")

        await asyncio.sleep(self.delay)
        await on_event("tool_end", {"tool_id": "t1", "tool_name": "command_execution", "output": "3 passed", "is_error": False})
        await on_event("assistant_message", {"text": f"已完成: {prompt}"})
        return TurnResult(
            status="completed",
            native_session_id=native_session_id or "sess_fake_1",
            final_text=f"已完成: {prompt}",
        )

    async def cancel_turn(self, bot_id: str, turn_id: str) -> None:
        key = (bot_id, turn_id)
        self.cancelled_keys.append(key)
        ev = self._cancel_events.get(key)
        if ev:
            ev.set()


def test_plugin_disabled_by_default():
    app = create_app(mode="full", config={})
    try:
        app.plugin_loader.load_plugin_by_entry("lite_agent.features.pc_cli", plugin_name="pc_cli")
        matched = app.route_registry.match("GET", "/api/v1/pc-cli/nodes") if hasattr(app.route_registry, "match") else None
        assert matched is None
    finally:
        app.shutdown()


def test_plugin_enabled_and_cleanup():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "pc_cli.db")
        app = create_app(
            mode="full",
            config={
                "pc_cli": {
                    "enabled": True,
                    "db_path": db_path,
                    "ws_host": "127.0.0.1",
                    "ws_port": 0,
                    "daemon_token": "test_daemon_tok",
                    "client_token": "test_client_tok",
                }
            },
        )
        try:
            app.plugin_loader.load_plugin_by_entry("lite_agent.features.pc_cli", plugin_name="pc_cli")
            assert "lite_agent.features.pc_cli" in app.loaded_plugins
            if hasattr(app.route_registry, "match"):
                assert app.route_registry.match("GET", "/api/v1/pc-cli/nodes") is not None
                assert app.route_registry.match("POST", "/api/v1/pc-cli/bots") is not None
        finally:
            app.shutdown()


def test_repository_events_and_approval_cas():
    repo = PcCliRepository(":memory:")
    try:
        repo.upsert_node("mac1", "Mac Studio", "darwin", ["codex", "agy"], ["/Users/dev"])
        bot = repo.create_bot(name="demo", node_id="mac1", engine="codex", cwd="/Users/dev/proj")
        ev1 = repo.append_event(bot_id=bot["bot_id"], turn_id="t1", event_type="user_message", payload={"text": "hi"})
        ev2 = repo.append_event(bot_id=bot["bot_id"], turn_id="t1", event_type="assistant_message", payload={"text": "ok"})
        assert ev2["seq_id"] > ev1["seq_id"]

        inc = repo.list_events(bot["bot_id"], after_seq=ev1["seq_id"])
        assert len(inc) == 1
        assert inc[0]["seq_id"] == ev2["seq_id"]

        appr = repo.create_approval(
            bot_id=bot["bot_id"],
            tool_name="delete_bot",
            summary="删除 demo",
            approval_type=ApprovalType.BOT_DELETE.value,
            requested_by="llm_manager",
        )
        ok1, res1 = repo.resolve_approval_cas(appr["approval_id"], "allow_once")
        ok2, res2 = repo.resolve_approval_cas(appr["approval_id"], "deny")
        assert ok1 is True and res1["decision"] == "allow_once"
        assert ok2 is False and res2["decision"] == "allow_once"
    finally:
        repo.close()


def test_codex_and_agy_parsers_and_args():
    codex = CodexCliAdapter(executable="codex", env={})
    c_new = codex.build_args(native_session_id=None, approval_mode="auto", model="o3")
    exec_idx = c_new.index("exec")
    assert c_new[exec_idx:exec_idx + 7] == [
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-c",
        'approval_policy="never"',
        "-c",
        'sandbox_mode="workspace-write"',
    ]
    assert "--full-auto" not in c_new
    assert c_new[-3:] == ["-m", "o3", "-"]
    c_res = codex.build_args(native_session_id="th_1", approval_mode="ask", model=None)
    res_idx = c_res.index("exec")
    assert c_res[res_idx:res_idx + 5] == ["exec", "resume", "--json", "--skip-git-repo-check", "--all"]
    assert c_res[-2:] == ["th_1", "-"]
    c_res_auto = codex.build_args(native_session_id="th_1", approval_mode="auto", model=None)
    assert 'approval_policy="never"' in c_res_auto and 'sandbox_mode="workspace-write"' in c_res_auto

    parsed = parse_codex_json_line('{"type":"thread.started","thread_id":"th_abc"}\n')
    assert parsed and parsed["thread_id"] == "th_abc"
    tool = extract_codex_tool_payload({"type": "command_execution", "id": "c1", "command": "ls", "exit_code": 0, "output": "a\nb"})
    assert tool and tool["tool_name"] == "command_execution" and tool["is_error"] is False
    assert extract_codex_tool_payload({"type": "error", "id": "item_0", "message": "deprecated config"}) is None

    agy = AgyCliAdapter(executable="agy", env={})
    conv_uuid = "12345678-1234-5678-1234-567812345678"
    agy_args = agy.build_args(cwd="/tmp/proj", native_session_id=conv_uuid, approval_mode="auto", model="gemini-pro")
    assert "--input-format" in agy_args and "text" in agy_args
    assert "--conversation" in agy_args and conv_uuid in agy_args
    assert "--add-dir" in agy_args and "/tmp/proj" in agy_args
    assert "--dangerously-skip-permissions" in agy_args and "--model" in agy_args
    assert parse_agy_json_line('{"conversation_id":"c1","text_delta":"hi"}')["text_delta"] == "hi"
    env_step = unwrap_agy_event({"event": "step_update", "step_update": {"text_delta": "hello", "step_index": 1}})
    assert env_step["text_delta"] == "hello"
    env_res = unwrap_agy_event({"event": "result", "result": {"finished_reason": "stop", "response": "done"}})
    assert env_res["response"] == "done"


def test_env_probe_path_safety():
    with tempfile.TemporaryDirectory() as tmp:
        sub = os.path.join(tmp, "proj_a")
        os.makedirs(sub)
        assert is_path_within_roots(sub, [tmp]) is True
        assert is_path_within_roots(os.path.dirname(tmp), [tmp]) is False

        ok, entries, err = list_safe_dirs(tmp, [tmp])
        assert ok is True and err == ""
        assert any(e["name"] == "proj_a" for e in entries)

        ok_bad, _, err_bad = list_safe_dirs(os.path.dirname(tmp), [tmp])
        assert ok_bad is False and "白名单" in err_bad


def test_end_to_end_ws_daemon_hub_and_client():
    """完整闭环：Hub + WS Server + Daemon (Fake Adapter) + Mobile WS Client。"""
    with tempfile.TemporaryDirectory() as tmp:
        proj_dir = os.path.join(tmp, "demo_repo")
        os.makedirs(proj_dir)

        repo = PcCliRepository(":memory:")
        hub = PcCliHub(repo=repo)
        ws_server = PcCliWsServer(
            hub=hub,
            host="127.0.0.1",
            port=0,
            daemon_token="secret_daemon_token",
            client_token="secret_client_token",
        )
        ws_server.start()

        async def _scenario():
            fake_codex = _FakeCliAdapter(delay=0.02)
            cfg = DaemonConfig(
                hub_ws_url=f"ws://127.0.0.1:{ws_server.bound_port}/ws/pc-cli/daemon",
                daemon_token="secret_daemon_token",
                node_id="win-test-1",
                display_name="Win Test PC",
                workspace_roots=[tmp],
                heartbeat_interval_sec=60.0,
            )
            daemon = PcCliDaemon(cfg, adapters={"codex": fake_codex, "agy": _FakeCliAdapter()})
            daemon_task = asyncio.create_task(daemon.run_once())

            # 等待节点上线
            for _ in range(50):
                if hub.is_node_online("win-test-1"):
                    break
                await asyncio.sleep(0.05)
            assert hub.is_node_online("win-test-1")

            # 目录安全查询（跨线程 RPC）
            ok_dir, dir_res = await asyncio.to_thread(hub.list_node_dirs, "win-test-1", tmp)
            assert ok_dir is True
            assert any(e["name"] == "demo_repo" for e in dir_res["entries"])

            # 创建 Bot
            ok_bot, bot_res = hub.create_bot(
                name="测试 Codex",
                node_id="win-test-1",
                engine="codex",
                cwd=proj_dir,
                approval_mode="auto",
            )
            assert ok_bot is True
            bot_id = bot_res["bot"]["bot_id"]

            # 手机端连接并首帧鉴权 + 订阅该 Bot
            client = await StdlibWsClient.connect(f"ws://127.0.0.1:{ws_server.bound_port}/ws/pc-cli/client")
            await client.send_json({"type": "auth", "token": "secret_client_token"})
            assert json.loads(await client.recv_text() or "{}")["type"] == "auth.ok"
            await client.send_json({"type": "subscribe", "bot_id": bot_id})
            assert json.loads(await client.recv_text() or "{}")["type"] == "subscribe.ok"

            # 下发回合
            ok_turn, turn_res = hub.start_turn(bot_id, "运行单测并汇报")
            assert ok_turn is True

            # 收集手机端收到的实时事件直到 turn_status completed
            received_types: List[str] = []
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(client.recv_text(), timeout=3.0)
                if not raw:
                    break
                msg = json.loads(raw)
                if msg.get("type") == "bot.event":
                    et = msg["event"]["event_type"]
                    received_types.append(et)
                    if et == "turn_status" and msg["event"]["payload"].get("status") == TurnStatus.COMPLETED.value:
                        break

            assert "thinking" in received_types
            assert "tool_start" in received_types
            assert "tool_end" in received_types
            assert "assistant_message" in received_types
            assert "turn_status" in received_types

            # 验证 native_session_id 已持久化供多轮续接
            bot_after = hub.repo.get_bot(bot_id)
            assert bot_after is not None
            assert bot_after["native_session_id"] == "sess_fake_1"
            assert bot_after["status"] == BotStatus.IDLE.value

            # 测试中止回合
            ok_t2, _ = hub.start_turn(bot_id, "WAIT_FOR_CANCEL")
            assert ok_t2 is True
            await asyncio.sleep(0.08)
            ok_c, _ = hub.cancel_turn(bot_id)
            assert ok_c is True
            for _ in range(40):
                b = hub.repo.get_bot(bot_id)
                if b and b["status"] == BotStatus.IDLE.value and len(fake_codex.cancelled_keys) == 1:
                    break
                await asyncio.sleep(0.05)
            assert len(fake_codex.cancelled_keys) == 1

            # 测试授权删除 Bot（bot_delete 审批单）
            del_appr = hub.repo.create_approval(
                bot_id=bot_id,
                tool_name="delete_bot",
                summary="AI 总管请求删除测试 Bot",
                approval_type=ApprovalType.BOT_DELETE.value,
                requested_by="llm_manager",
            )
            ok_res, res_data = hub.resolve_approval(del_appr["approval_id"], "allow_once")
            assert ok_res is True and res_data["executed"] is True
            assert hub.repo.get_bot(bot_id) is None

            await client.close()
            daemon.request_stop()
            await asyncio.wait_for(daemon_task, timeout=3.0)

        try:
            asyncio.run(_scenario())
        finally:
            ws_server.stop()
            hub.close()


def test_windows_cmd_fallback_rejected_and_model_validated():
    """复核点 1：无法拆包的 .cmd/.bat/.ps1 必须 fail-closed，且 model 参数拒绝 shell 元字符。"""
    with tempfile.TemporaryDirectory() as tmp:
        raw_cmd = os.path.join(tmp, "arbitrary.cmd")
        with open(raw_cmd, "w", encoding="utf-8") as f:
            f.write("@echo off\necho hi\n")
        with pytest.raises(ValueError, match="命令解释器"):
            build_spawn_argv(raw_cmd, ["--json"], env={}, platform="win32")

        ps1 = os.path.join(tmp, "script.ps1")
        with open(ps1, "w", encoding="utf-8") as f:
            f.write("Write-Host hi\n")
        with pytest.raises(ValueError, match="PowerShell"):
            build_spawn_argv(ps1, [], env={}, platform="win32")

    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        repo.upsert_node("win1", "Win", "win32", ["codex"], ["C:\\proj"])
        ok, res = hub.create_bot(name="bad", node_id="win1", engine="codex", cwd="C:\\proj", model="o3 & calc.exe")
        assert ok is False and "无效的模型名" in res["error"]
    finally:
        hub.close()


def test_daemon_ownership_and_late_event_rejected():
    """复核点 2 & 3：校验发送连接归属、跨节点隔离、已终结回合与已删除 Bot 迟到消息丢弃。"""
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        hub.handle_daemon_hello("conn_a1", lambda _: None, {"node_id": "node_a", "engines": ["codex"], "workspace_roots": ["/a"]})
        hub.handle_daemon_hello("conn_b1", lambda _: None, {"node_id": "node_b", "engines": ["codex"], "workspace_roots": ["/b"]})

        ok_bot, bot_res = hub.create_bot(name="botA", node_id="node_a", engine="codex", cwd="/a")
        assert ok_bot
        bot_id = bot_res["bot"]["bot_id"]

        ok_turn, turn_res = hub.start_turn(bot_id, "hello")
        assert ok_turn
        turn_id = turn_res["turn"]["turn_id"]

        # 1. 跨节点伪造事件必须被拒绝
        ev_spoof = hub.handle_daemon_event(
            {"bot_id": bot_id, "turn_id": turn_id, "event_type": "assistant_message", "payload": {"text": "hack"}},
            sender_node_id="node_b",
            conn_id="conn_b1",
        )
        assert ev_spoof is None

        # 2. 旧连接 conn_id 必须被拒绝
        ev_stale = hub.handle_daemon_event(
            {"bot_id": bot_id, "turn_id": turn_id, "event_type": "assistant_message", "payload": {"text": "old"}},
            sender_node_id="node_a",
            conn_id="conn_a_old",
        )
        assert ev_stale is None

        # 3. 合法归属连接完成回合后，迟到事件与二次 finished 必须被丢弃
        hub.handle_daemon_turn_finished(
            {"bot_id": bot_id, "turn_id": turn_id, "status": "completed", "native_session_id": "s1"},
            sender_node_id="node_a",
            conn_id="conn_a1",
        )
        events_before = hub.repo.list_events(bot_id)
        late_ev = hub.handle_daemon_event(
            {"bot_id": bot_id, "turn_id": turn_id, "event_type": "assistant_message", "payload": {"text": "late"}},
            sender_node_id="node_a",
            conn_id="conn_a1",
        )
        assert late_ev is None
        hub.handle_daemon_turn_finished(
            {"bot_id": bot_id, "turn_id": turn_id, "status": "failed", "error": "late_fail"},
            sender_node_id="node_a",
            conn_id="conn_a1",
        )
        assert hub.repo.get_bot(bot_id)["status"] == BotStatus.IDLE.value
        assert len(hub.repo.list_events(bot_id)) == len(events_before)

        # 4. 删除 Bot 后迟到消息不得写入孤立事件
        ok_t2, t2_res = hub.start_turn(bot_id, "second")
        assert ok_t2
        t2_id = t2_res["turn"]["turn_id"]
        hub.delete_bot(bot_id)
        hub.handle_daemon_turn_finished(
            {"bot_id": bot_id, "turn_id": t2_id, "status": "completed"},
            sender_node_id="node_a",
            conn_id="conn_a1",
        )
        assert hub.repo.list_events(bot_id) == []
    finally:
        hub.close()


def test_send_failure_rolls_back_turn_and_cancel_semantics():
    """复核点 3：WS 发送失败时 start_turn 立刻回滚，且在线中止返回 cancel_requested 语义。"""
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        def _failing_send(_msg):
            raise ConnectionError("socket broken")

        hub.handle_daemon_hello("conn_broken", _failing_send, {"node_id": "n1", "engines": ["codex"], "workspace_roots": ["/p"]})
        ok_b, b_res = hub.create_bot(name="b1", node_id="n1", engine="codex", cwd="/p")
        assert ok_b
        bot_id = b_res["bot"]["bot_id"]

        ok_t, t_res = hub.start_turn(bot_id, "ping")
        assert ok_t is False and "失败" in t_res["error"]
        assert hub.repo.get_active_turn_for_bot(bot_id) is None

        # 替换为健康连接测试在线中止语义
        hub.handle_daemon_hello("conn_ok", lambda _: None, {"node_id": "n1", "engines": ["codex"], "workspace_roots": ["/p"]})
        ok_t2, _ = hub.start_turn(bot_id, "run")
        assert ok_t2 is True
        ok_c, c_res = hub.cancel_turn(bot_id)
        assert ok_c is True
        assert c_res["cancel_requested"] is True
        assert c_res["cancelled"] is False
    finally:
        hub.close()


def test_adapter_total_timeout_covers_post_stdout_hang():
    """复核点 4：子进程关闭 stdout 后仍挂起不退出时，总超时仍能强杀进程树并返回 failed。"""
    with tempfile.TemporaryDirectory() as tmp:
        # 使用真实 Python 子进程：关闭 stdout 后 sleep 30 秒
        class _HangingCodex(CodexCliAdapter):
            def build_args(self, **kwargs):
                return [sys.executable, "-c", "import sys, time; sys.stdout.close(); time.sleep(30)"]

        adapter = _HangingCodex(executable=sys.executable, env=dict(os.environ), inactivity_timeout_sec=10.0, turn_timeout_sec=0.6)

        async def _run():
            t0 = time.monotonic()
            res = await adapter.run_turn(
                bot_id="b_hang",
                turn_id="t_hang",
                cwd=tmp,
                prompt="hi",
                native_session_id=None,
                approval_mode="auto",
                model=None,
                on_event=lambda _t, _p: asyncio.sleep(0),
            )
            elapsed = time.monotonic() - t0
            return res, elapsed

        res, elapsed = asyncio.run(_run())
        assert res.status == "failed"
        assert "超时" in (res.error or "")
        assert elapsed < 5.0

