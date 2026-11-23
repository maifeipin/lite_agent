"""
PC CLI Phase 2 单元测试、并发安全与 PWA 静态资产校验 (tests/test_pc_cli_phase2.py).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from lite_agent.contracts.message import AgentResponse
from lite_agent.features.pc_cli.hub import PcCliHub
from lite_agent.features.pc_cli.notifier import PcCliNotifier
from lite_agent.features.pc_cli.repository import PcCliRepository

REPO_ROOT = Path(__file__).resolve().parents[3]
MOBILE_DIR = REPO_ROOT / "web_dashboard" / "mobile"


def test_notifier_agent_response_return_value_and_async():
    received: list[AgentResponse] = []

    def _ok_channel(resp: AgentResponse) -> bool:
        assert isinstance(resp, AgentResponse)
        assert hasattr(resp, "title") and hasattr(resp, "text") and hasattr(resp, "color")
        received.append(resp)
        return True

    notifier = PcCliNotifier(
        send_fn=_ok_channel,
        notify_on_complete=True,
        notify_on_approval=False,
        mobile_base_url="https://mail.example.com/mobile/",
    )
    try:
        assert notifier.notify("Bot [demo] completed", "全部测试通过", kind="complete", bot_id="bot_123") is True
        assert len(received) == 1
        assert received[0].title == "PC CLI · Bot [demo] completed"
        assert received[0].color == "blue"
        assert "https://mail.example.com/mobile/#bot/bot_123" in received[0].text

        # approval 开关关闭时不推送
        assert notifier.notify("待审批", "run rm", kind="approval", bot_id="bot_123") is False
        assert len(received) == 1

        # 异步投递不阻塞主线程
        t0 = time.monotonic()
        assert notifier.notify_async("Bot [demo] completed", "后台推送", kind="complete", bot_id="bot_123") is True
        assert (time.monotonic() - t0) < 0.2
        for _ in range(30):
            if len(received) == 2:
                break
            time.sleep(0.02)
        assert len(received) == 2
    finally:
        notifier.close()

    # 宿主投递返回 False 时，notify 必须如实返回 False
    fail_notifier = PcCliNotifier(send_fn=lambda _resp: False)
    try:
        assert fail_notifier.notify("t", "c") is False
    finally:
        fail_notifier.close()

    # 慢通道异步投递不阻塞调用方
    slow_done = threading.Event()

    def _slow_channel(_resp: AgentResponse) -> bool:
        time.sleep(0.3)
        slow_done.set()
        return True

    slow_notifier = PcCliNotifier(send_fn=_slow_channel)
    try:
        t0 = time.monotonic()
        assert slow_notifier.notify_async("slow", "msg") is True
        assert (time.monotonic() - t0) < 0.1
        assert slow_done.wait(timeout=2.0)
    finally:
        slow_notifier.close()


def test_hub_triggers_notifier_with_agent_response():
    received: list[AgentResponse] = []
    notifier = PcCliNotifier(
        send_fn=lambda resp: (received.append(resp), True)[1],
        mobile_base_url="https://mail.example.com/mobile",
    )
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo, notifier=notifier.notify_async)
    try:
        hub.handle_daemon_hello("c1", lambda _: None, {"node_id": "mac1", "engines": ["codex"], "workspace_roots": ["/w"]})
        ok_b, b_res = hub.create_bot(name="mac-codex", node_id="mac1", engine="codex", cwd="/w")
        assert ok_b
        bot_id = b_res["bot"]["bot_id"]

        ok_t, t_res = hub.start_turn(bot_id, "run tests")
        assert ok_t
        turn_id = t_res["turn"]["turn_id"]

        hub.handle_daemon_approval_request(
            {"bot_id": bot_id, "turn_id": turn_id, "tool_name": "run_command", "summary": "pytest -q"},
            sender_node_id="mac1",
            conn_id="c1",
        )
        hub.handle_daemon_turn_finished(
            {"bot_id": bot_id, "turn_id": turn_id, "status": "completed"},
            sender_node_id="mac1",
            conn_id="c1",
        )

        for _ in range(50):
            if len(received) == 2:
                break
            time.sleep(0.02)

        assert len(received) == 2
        assert received[0].color == "orange" and f"#bot/{bot_id}" in received[0].text
        assert received[1].color == "blue" and f"#bot/{bot_id}" in received[1].text
    finally:
        notifier.close()
        hub.close()


def test_concurrent_start_turn_allows_only_one():
    """多线程并发对同一 Bot 派发回合，仅允许 1 个成功，其余全部拒绝。"""
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        hub.handle_daemon_hello("c1", lambda _: None, {"node_id": "n1", "engines": ["codex"], "workspace_roots": ["/w"]})
        ok_b, b_res = hub.create_bot(name="race", node_id="n1", engine="codex", cwd="/w")
        assert ok_b
        bot_id = b_res["bot"]["bot_id"]

        barrier = threading.Barrier(8)
        results: list[bool] = []
        res_lock = threading.Lock()

        def _worker(idx: int):
            barrier.wait()
            ok, _ = hub.start_turn(bot_id, f"prompt-{idx}")
            with res_lock:
                results.append(ok)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1
        assert results.count(False) == 7
    finally:
        hub.close()


def test_mobile_pwa_assets_security_and_pagination_guards():
    manifest_path = MOBILE_DIR / "manifest.json"
    index_path = MOBILE_DIR / "index.html"
    store_js = (MOBILE_DIR / "store.js").read_text(encoding="utf-8")
    app_js = (MOBILE_DIR / "app.js").read_text(encoding="utf-8")
    html = index_path.read_text(encoding="utf-8")

    assert manifest_path.is_file()
    assert (MOBILE_DIR / "vendor" / "vue.esm-browser.prod.js").is_file()
    assert (MOBILE_DIR / "vendor" / "marked.min.js").is_file()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/mobile/"

    # 1. 无外网 CDN
    assert "unpkg.com" not in html
    assert "cdn.jsdelivr.net" not in html
    # 2. 独立历史同步游标 + 逐页循环 + 旧 WS 隔离
    assert "historySyncSeqByBot" in store_js
    assert "advanceHistoryCursor" in store_js
    assert store_js.count("if (_ws !== ws) return;") >= 3
    # 3. Markdown URL 协议安全过滤
    assert "sanitizeRenderedHtml" in app_js
    assert "isSafeUrl" in app_js
    # 4. Phase 3 已接通交互式审批，ask 选项已启用，且 AGY 不展示“本会话允许”
    assert '<option value="ask">' in html
    assert "activeBot && activeBot.engine === 'codex'" in html
    assert "subscribe.ok" in store_js
    assert "await subscribeBotOnWs(botId)" in app_js
