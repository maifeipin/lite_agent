"""
PC CLI Phase 3 交互式审批与边界回归测试 (tests/test_pc_cli_phase3.py).
使用真实 Python 子进程模拟 Codex `app-server` JSON-RPC 2.0 与 AGY `acp` JSON-RPC 2.0，
完整验证：子进程发起权限审批 -> Daemon -> WS -> Hub -> 手机决策 -> Daemon -> 子进程继续执行。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List

from lite_agent.contracts.message import AgentResponse
from lite_agent.features.pc_cli.contracts import BotStatus, TurnStatus
from lite_agent.features.pc_cli.daemon.adapters.agy_adapter import AgyCliAdapter, select_acp_permission_outcome
from lite_agent.features.pc_cli.daemon.adapters.codex_adapter import (
    CodexCliAdapter,
    build_codex_permissions_response,
    map_decision_to_codex,
)
from lite_agent.features.pc_cli.daemon.config import DaemonConfig
from lite_agent.features.pc_cli.daemon.daemon import PcCliDaemon
from lite_agent.features.pc_cli.hub import PcCliHub
from lite_agent.features.pc_cli.notifier import PcCliNotifier
from lite_agent.features.pc_cli.repository import PcCliRepository
from lite_agent.features.pc_cli.ws_server import PcCliWsServer


_FAKE_CODEX_APP_SERVER_SCRIPT = r"""
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    m = msg.get("method")
    rid = msg.get("id")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"serverInfo": {"name": "fake-codex"}}})
    elif m == "thread/start":
        send({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": "th_rpc_1"}}})
    elif m == "turn/start":
        send({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": "nt_1"}}})
        # 反向请求权限审批
        send({
            "jsonrpc": "2.0",
            "id": 9001,
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "rm -rf ./tmp_build", "reason": "clean build"},
        })
    elif rid == 9001 and "result" in msg:
        dec = msg["result"].get("decision")
        send({
            "jsonrpc": "2.0",
            "method": "item/completed",
            "params": {"item": {"type": "agent_message", "text": f"CODEX_DECISION:{dec}"}},
        })
        send({
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {"turn": {"id": "nt_1", "status": "completed"}},
        })
        break
"""

_FAKE_AGY_ACP_SCRIPT = r"""
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

prompt_req_id = None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    m = msg.get("method")
    rid = msg.get("id")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1}})
    elif m == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "11111111-2222-3333-4444-555555555555"}})
    elif m == "session/prompt":
        prompt_req_id = rid
        send({
            "jsonrpc": "2.0",
            "id": 7001,
            "method": "session/request_permission",
            "params": {
                "sessionId": "11111111-2222-3333-4444-555555555555",
                "toolCall": {"title": "run_shell", "rawInput": "deploy.sh"},
                "options": [
                    {"optionId": "opt_allow", "kind": "allow_once", "name": "Allow"},
                    {"optionId": "opt_reject", "kind": "reject_once", "name": "Reject"},
                ],
            },
        })
    elif rid == 7001 and "result" in msg:
        chosen = msg["result"].get("outcome", {}).get("optionId", "none")
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": f"AGY_OUTCOME:{chosen}"}}},
        })
        send({"jsonrpc": "2.0", "id": prompt_req_id, "result": {"stopReason": "end_turn"}})
        break
"""


def test_decision_mappers():
    assert map_decision_to_codex("allow_once") == "accept"
    assert map_decision_to_codex("allow_session") == "acceptForSession"
    assert map_decision_to_codex("deny") == "decline"

    # Codex item/permissions/requestApproval 协议格式 (permissions + scope)
    req_params = {"permissions": {"network": {"enabled": True}}}
    assert build_codex_permissions_response("allow_once", req_params) == {
        "permissions": {"network": {"enabled": True}},
        "scope": "turn",
    }
    assert build_codex_permissions_response("allow_session", req_params) == {
        "permissions": {"network": {"enabled": True}},
        "scope": "session",
    }
    assert build_codex_permissions_response("deny", req_params) == {"permissions": {}, "scope": "turn"}

    # AGY ACP 严格最小权限：绝不扩大为 allow_always
    opts_with_once = [
        {"optionId": "o1", "kind": "allow_once"},
        {"optionId": "o2", "kind": "allow_always"},
        {"optionId": "o3", "kind": "reject_once"},
    ]
    assert select_acp_permission_outcome("allow_once", opts_with_once)["outcome"]["optionId"] == "o1"
    assert select_acp_permission_outcome("allow_session", opts_with_once)["outcome"]["optionId"] == "o1"
    assert select_acp_permission_outcome("deny", opts_with_once)["outcome"]["optionId"] == "o3"

    # 仅提供 allow_always 时，必须拒绝/取消，严禁升级为永久授权
    opts_only_always = [
        {"optionId": "o_always", "kind": "allow_always"},
        {"optionId": "o_reject", "kind": "reject_once"},
    ]
    assert select_acp_permission_outcome("allow_once", opts_only_always)["outcome"]["optionId"] == "o_reject"
    assert select_acp_permission_outcome("allow_session", opts_only_always)["outcome"]["optionId"] == "o_reject"
    assert select_acp_permission_outcome("allow_once", [{"optionId": "o_always", "kind": "allow_always"}]) == {
        "outcome": {"outcome": "cancelled"}
    }


def test_approval_expires_on_disconnect_and_send_failure():
    """复核点 3：节点断线时审批单自动失效；WS 发送失败时回标 expired，绝不误写 running。"""
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        hub.handle_daemon_hello("c1", lambda _: None, {"node_id": "n1", "engines": ["codex"], "workspace_roots": ["/w"]})
        ok_b, b_res = hub.create_bot(name="b1", node_id="n1", engine="codex", cwd="/w", approval_mode="ask")
        assert ok_b
        bot_id = b_res["bot"]["bot_id"]

        ok_t, t_res = hub.start_turn(bot_id, "run")
        assert ok_t
        turn_id = t_res["turn"]["turn_id"]

        appr = hub.handle_daemon_approval_request(
            {"bot_id": bot_id, "turn_id": turn_id, "tool_name": "run_command", "summary": "ls"},
            sender_node_id="n1",
            conn_id="c1",
        )
        assert appr is not None

        # 1) 节点断线 -> 审批单自动失效
        hub.handle_daemon_disconnect("n1", "c1")
        assert hub.repo.get_approval(appr["approval_id"])["decision"] == "expired"
        ok_late, late_res = hub.resolve_approval(appr["approval_id"], "allow_once")
        assert ok_late is False

        # 2) 重连但发送审批决策时底层抛异常 -> 回标 expired 且不写回 running
        def _fail_on_approval(msg):
            if msg.get("type") == "turn.approval_decision":
                raise ConnectionError("broken pipe")

        hub.handle_daemon_hello("c2", _fail_on_approval, {"node_id": "n1", "engines": ["codex"], "workspace_roots": ["/w"]})
        appr2 = hub.handle_daemon_approval_request(
            {"bot_id": bot_id, "turn_id": turn_id, "tool_name": "run_command", "summary": "ls2"},
            sender_node_id="n1",
            conn_id="c2",
        )
        assert appr2 is not None
        assert hub.repo.get_bot(bot_id)["status"] == BotStatus.WAITING_APPROVAL.value

        ok_res, res_data = hub.resolve_approval(appr2["approval_id"], "allow_once")
        assert ok_res is False
        assert res_data["delivered"] is False
        assert hub.repo.get_approval(appr2["approval_id"])["decision"] == "expired"
        assert hub.repo.get_bot(bot_id)["status"] == BotStatus.WAITING_APPROVAL.value

        # 3) Daemon 返回 accepted=False (如本地 Future 已过期) -> Hub 拒绝并回标 expired
        def _reject_ack(msg):
            if msg.get("type") == "turn.approval_decision":
                hub.handle_daemon_approval_ack(
                    {
                        "bot_id": msg["bot_id"],
                        "turn_id": msg["turn_id"],
                        "approval_id": msg["approval_id"],
                        "accepted": False,
                        "reason": "approval_not_pending_on_daemon",
                    },
                    sender_node_id="n1",
                    conn_id="c3",
                )

        hub.handle_daemon_hello("c3", _reject_ack, {"node_id": "n1", "engines": ["codex"], "workspace_roots": ["/w"]})
        appr3 = hub.handle_daemon_approval_request(
            {"bot_id": bot_id, "turn_id": turn_id, "tool_name": "run_command", "summary": "ls3"},
            sender_node_id="n1",
            conn_id="c3",
        )
        assert appr3 is not None
        ok_res3, res_data3 = hub.resolve_approval(appr3["approval_id"], "allow_once")
        assert ok_res3 is False
        assert res_data3["delivered"] is False
        assert "approval_not_pending_on_daemon" in res_data3["error"]
        assert hub.repo.get_approval(appr3["approval_id"])["decision"] == "expired"
        assert hub.repo.get_bot(bot_id)["status"] == BotStatus.WAITING_APPROVAL.value
    finally:
        hub.close()


def test_legacy_db_duplicate_active_turns_migration():
    """复核点 2：旧库已存在同一 Bot 多条 running 回合时，启动迁移自动收敛并成功建唯一索引。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE cli_turns (
                turn_id TEXT PRIMARY KEY,
                bot_id TEXT NOT NULL,
                prompt TEXT NOT NULL,
                input_source TEXT NOT NULL DEFAULT 'text',
                status TEXT NOT NULL DEFAULT 'queued',
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                error_message TEXT
            );
            INSERT INTO cli_turns VALUES ('t1', 'b1', 'p1', 'text', 'running', 100, NULL, NULL);
            INSERT INTO cli_turns VALUES ('t2', 'b1', 'p2', 'text', 'running', 200, NULL, NULL);
            """
        )
        conn.commit()
        conn.close()

        repo = PcCliRepository(db_path)
        try:
            t1 = repo.get_turn("t1")
            t2 = repo.get_turn("t2")
            assert t1["status"] == "failed" and t1["error_message"] == "migration_dedup_stale_active_turn"
            assert t2["status"] == "running"
        finally:
            repo.close()


def test_notifier_close_when_queue_is_full():
    """复核点 3：队列满时调用 close() 仍能可靠退出后台线程，不泄漏。"""
    gate = threading.Event()

    def _blocking_send(_resp: AgentResponse) -> bool:
        gate.wait(timeout=0.3)
        return True

    notifier = PcCliNotifier(send_fn=_blocking_send, max_queue_size=2)
    assert notifier.notify_async("1", "a") is True
    assert notifier.notify_async("2", "b") is True
    assert notifier.notify_async("3", "c") in (True, False)  # 填满队列
    gate.set()
    notifier.close(timeout=2.0)
    assert notifier._worker is not None and not notifier._worker.is_alive()


def test_phase3_end_to_end_codex_and_agy_interactive_approvals():
    """端到端真实子进程测试：Codex app-server 允许审批 + AGY acp 拒绝审批。"""
    with tempfile.TemporaryDirectory() as tmp:
        codex_script = os.path.join(tmp, "fake_codex.py")
        agy_script = os.path.join(tmp, "fake_agy.py")
        with open(codex_script, "w", encoding="utf-8") as f:
            f.write(_FAKE_CODEX_APP_SERVER_SCRIPT)
        with open(agy_script, "w", encoding="utf-8") as f:
            f.write(_FAKE_AGY_ACP_SCRIPT)

        class _TestCodexAdapter(CodexCliAdapter):
            def build_app_server_args(self) -> List[str]:
                return [sys.executable, codex_script]

        class _TestAgyAdapter(AgyCliAdapter):
            def build_acp_args(self, *, cwd: str, approval_mode: str) -> List[str]:
                return [sys.executable, agy_script]

        repo = PcCliRepository(":memory:")
        hub = PcCliHub(repo=repo)
        ws_server = PcCliWsServer(
            hub=hub,
            host="127.0.0.1",
            port=0,
            daemon_token="tok_p3_daemon",
            client_token="tok_p3_client",
        )
        ws_server.start()

        async def _scenario():
            cfg = DaemonConfig(
                hub_ws_url=f"ws://127.0.0.1:{ws_server.bound_port}/ws/pc-cli/daemon",
                daemon_token="tok_p3_daemon",
                node_id="pc-p3",
                display_name="PC Phase 3",
                workspace_roots=[tmp],
                heartbeat_interval_sec=60.0,
            )
            daemon = PcCliDaemon(
                cfg,
                adapters={
                    "codex": _TestCodexAdapter(executable=sys.executable, env=dict(os.environ), turn_timeout_sec=15.0),
                    "agy": _TestAgyAdapter(executable=sys.executable, env=dict(os.environ), turn_timeout_sec=15.0),
                },
            )
            daemon_task = asyncio.create_task(daemon.run_once())

            for _ in range(50):
                if hub.is_node_online("pc-p3"):
                    break
                await asyncio.sleep(0.05)
            assert hub.is_node_online("pc-p3")

            # --- 1) Codex app-server: ask 模式 -> 允许审批 ---
            ok_c, bot_c = hub.create_bot(name="codex-ask", node_id="pc-p3", engine="codex", cwd=tmp, approval_mode="ask")
            assert ok_c
            bot_c_id = bot_c["bot"]["bot_id"]

            ok_tc, _ = hub.start_turn(bot_c_id, "clean build")
            assert ok_tc

            # 等待审批单到达 Hub
            approvals = []
            for _ in range(60):
                approvals = hub.repo.list_pending_approvals(bot_c_id)
                if approvals:
                    break
                await asyncio.sleep(0.05)
            assert len(approvals) == 1
            assert "rm -rf" in approvals[0]["summary"]
            assert hub.repo.get_bot(bot_c_id)["status"] == BotStatus.WAITING_APPROVAL.value

            # 手机端点击“允许本次”（在独立线程调用以模拟真实 REST 线程等待 Daemon ack）
            ok_dec, dec_res = await asyncio.to_thread(hub.resolve_approval, approvals[0]["approval_id"], "allow_once")
            assert ok_dec is True and dec_res["delivered"] is True

            # 等待 Codex 子进程收到 accept 并完成回合
            for _ in range(60):
                if hub.repo.get_bot(bot_c_id)["status"] == BotStatus.IDLE.value:
                    break
                await asyncio.sleep(0.05)
            events_c = hub.repo.list_events(bot_c_id)
            assert any("CODEX_DECISION:accept" in str(e["payload"].get("text", "")) for e in events_c)
            assert hub.repo.get_bot(bot_c_id)["native_session_id"] == "th_rpc_1"

            # --- 2) AGY acp: ask 模式 -> 拒绝审批 ---
            ok_a, bot_a = hub.create_bot(name="agy-ask", node_id="pc-p3", engine="agy", cwd=tmp, approval_mode="ask")
            assert ok_a
            bot_a_id = bot_a["bot"]["bot_id"]

            ok_ta, _ = hub.start_turn(bot_a_id, "deploy now")
            assert ok_ta

            approvals_a = []
            for _ in range(60):
                approvals_a = hub.repo.list_pending_approvals(bot_a_id)
                if approvals_a:
                    break
                await asyncio.sleep(0.05)
            assert len(approvals_a) == 1
            assert "deploy.sh" in approvals_a[0]["summary"]

            # 手机端点击“拒绝”
            ok_dec_a, dec_res_a = await asyncio.to_thread(hub.resolve_approval, approvals_a[0]["approval_id"], "deny")
            assert ok_dec_a is True and dec_res_a["delivered"] is True

            for _ in range(60):
                if hub.repo.get_bot(bot_a_id)["status"] == BotStatus.IDLE.value:
                    break
                await asyncio.sleep(0.05)
            events_a = hub.repo.list_events(bot_a_id)
            assert any("AGY_OUTCOME:opt_reject" in str(e["payload"].get("text", "")) for e in events_a)
            assert hub.repo.get_bot(bot_a_id)["native_session_id"] == "11111111-2222-3333-4444-555555555555"

            daemon.request_stop()
            await asyncio.wait_for(daemon_task, timeout=3.0)

        try:
            asyncio.run(_scenario())
        finally:
            ws_server.stop()
            hub.close()
