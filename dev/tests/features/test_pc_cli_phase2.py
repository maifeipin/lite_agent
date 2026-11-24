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


def test_manager_resolves_invoker_and_provider_from_router_when_gateway_has_no_router(tmp_path):
    """回归：真实宿主 LLMGateway.router 为 None 时，需从宿主 router 解析 invoker 与 provider 传入 invoke_sync。"""
    from lite_agent.features.pc_cli.hub import PcCliHub
    from lite_agent.features.pc_cli.manager import PcCliManager
    from lite_agent.features.pc_cli.repository import PcCliRepository

    repo = PcCliRepository(db_path=str(tmp_path / "pc_cli.db"))
    hub = PcCliHub(repo=repo)
    try:
        captured = {}

        class _FakeInvoker:
            provider = "gemini"

        class _FakeRouter:
            def get_invoker(self, model_name, timeout=None):
                captured["router_model"] = model_name
                captured["router_timeout"] = timeout
                return _FakeInvoker()

            def get_driver(self, model_name):
                return "gemini"

        class _FakeGateway:
            router = None

            def invoke_sync(self, messages, **kwargs):
                if kwargs.get("invoker") is None:
                    raise ValueError("model or invoker is required")
                captured["invoke_kwargs"] = kwargs
                return {"content": "好的，已为您规划账单查询 Bot。", "tool_calls": [], "usage_total": 42}

        cfg = {"llm": {"default": "flash", "models": {"flash": {"driver": "gemini", "model": "gemini-2.5-flash"}}}}
        mgr = PcCliManager(hub=hub, config=cfg, gateway=_FakeGateway(), router=_FakeRouter(), default_model="flash")
        ok, payload = mgr.chat("创建一个查询账单的Bot。", model_name="flash")
        assert ok is True
        assert "账单" in payload["reply"]
        assert captured["router_model"] == "flash"
        assert captured["invoke_kwargs"]["provider"] == "gemini"
        assert captured["invoke_kwargs"]["invoker"] is not None
    finally:
        hub.close()


def test_plugin_register_binds_host_request_model_router_and_manager_handles_router_errors(tmp_path):
    """回归：[P1] register() 从真实宿主取得路由且默认模型保持配置别名；[P2] get_invoker 异常返回结构化错误；[P3] 通过真实注册路由验证端到端接线。"""
    import io
    import json
    from urllib.parse import urlparse
    from lite_agent.features.pc_cli import register as register_pc_cli

    routes = {}

    class _FakeInvoker:
        provider = "gemini"

    class _FakeHostRouter:
        default_model = "flash"

        def __init__(self):
            self.fail_next = False
            self.requested_models = []

        def get_invoker(self, model_name, timeout=None):
            self.requested_models.append(model_name)
            if self.fail_next:
                raise RuntimeError("missing GEMINI_API_KEY")
            return _FakeInvoker()

        def get_driver(self, model_name):
            return "gemini"

    class _FakeHostGateway:
        router = None

        def invoke_sync(self, messages, **kwargs):
            if kwargs.get("invoker") is None:
                raise ValueError("model or invoker is required")
            return {"content": "宿主路由绑定成功", "tool_calls": [], "usage_total": 18}

    host_router = _FakeHostRouter()

    class _FakeHostAgent:
        # 模拟真实 LiteAgent：.model 为底层 SDK 模型名（非配置别名），无公开 .router 属性
        model = "gemini-2.5-flash"
        llm = _FakeHostGateway()
        _request_model_router = None

        def _get_request_model_router(self):
            return host_router

    class _FakeContext:
        agent = _FakeHostAgent()
        config = {
            "pc_cli": {
                "enabled": True,
                "db_path": str(tmp_path / "pc_cli_reg.db"),
                "start_ws_server": False,
            },
            "llm": {
                "default": "flash",
                "models": {"flash": {"driver": "gemini", "model": "gemini-2.5-flash"}},
            },
        }

        def register_route(self, method, path, handler, **kwargs):
            routes[(method, path)] = handler

        def register_cleanup(self, name, fn=None):
            self._cleanup = fn if callable(fn) else name

    def _call_route(method, path, payload=None):
        raw = json.dumps(payload).encode("utf-8") if payload is not None else b""

        class _FakeHandler:
            command = method
            headers = {"Content-Length": str(len(raw))}
            rfile = io.BytesIO(raw)
            wfile = io.BytesIO()
            status = None

            def send_response(self, code):
                self.status = code

            def send_header(self, k, v):
                pass

            def end_headers(self):
                pass

        h = _FakeHandler()
        routes[(method, path)](h, urlparse(path))
        return h.status, json.loads(h.wfile.getvalue().decode("utf-8"))

    ctx = _FakeContext()
    register_pc_cli(ctx)
    try:
        # 1. 验证 GET /api/v1/pc-cli/models 默认模型保持配置别名 'flash'（而非 agent.model 的 'gemini-2.5-flash'）
        st_m, models_body = _call_route("GET", "/api/v1/pc-cli/models")
        assert st_m == 200
        assert models_body["default_model"] == "flash"

        # 2. 不传 model_name，验证默认别名 'flash' 正常命中宿主路由并调用成功
        st1, res1 = _call_route("POST", "/api/v1/pc-cli/manager/chat", {"message": "你好"})
        assert st1 == 200
        assert res1["success"] is True
        assert res1["reply"] == "宿主路由绑定成功"
        assert host_router.requested_models == ["flash"]

        # 3. 验证 get_invoker 抛异常时返回 400 结构化错误而非未捕获 500
        host_router.fail_next = True
        st2, res2 = _call_route("POST", "/api/v1/pc-cli/manager/chat", {"message": "再来一次"})
        assert st2 == 400
        assert res2["success"] is False
        assert "missing GEMINI_API_KEY" in res2["error"]
    finally:
        if hasattr(ctx, "_cleanup"):
            ctx._cleanup()
