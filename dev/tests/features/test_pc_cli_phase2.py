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


def test_manager_normalizes_flat_tool_calls_to_openai_wire_format_for_round2(tmp_path):
    """回归：model_invoker 返回扁平 tool_calls ({id, name, arguments})，第 2 轮回传 messages 时必须规范化为含 type='function' 的 OpenAI 结构。"""
    from lite_agent.features.pc_cli.hub import PcCliHub
    from lite_agent.features.pc_cli.manager import PcCliManager
    from lite_agent.features.pc_cli.repository import PcCliRepository

    repo = PcCliRepository(db_path=str(tmp_path / "pc_cli_tc.db"))
    hub = PcCliHub(repo=repo)
    try:
        seen_rounds = []

        class _StrictOpenAIGateway:
            def invoke_sync(self, messages, **kwargs):
                seen_rounds.append([dict(m) for m in messages])
                if len(seen_rounds) == 1:
                    # 模拟 OpenAIInvoker.invoke_sync 返回的扁平结构（无 type / function）
                    return {
                        "content": "",
                        "tool_calls": [{"id": "call_nodes_1", "name": "list_nodes", "arguments": "{}"}],
                        "usage_total": 30,
                    }
                # 第 2 轮严格校验 OpenAI 反序列化契约：assistant.tool_calls 必须含 type='function' 与 function 对象
                asst_msg = [m for m in messages if m.get("role") == "assistant"][-1]
                tcs = asst_msg.get("tool_calls") or []
                assert len(tcs) == 1
                assert tcs[0]["type"] == "function"
                assert tcs[0]["id"] == "call_nodes_1"
                assert tcs[0]["function"]["name"] == "list_nodes"
                assert tcs[0]["function"]["arguments"] == "{}"
                return {"content": "已查询到在线节点。", "tool_calls": [], "usage_total": 25}

        cfg = {"llm": {"default": "flash", "models": {"flash": {"driver": "openai", "model": "flash"}}}}
        mgr = PcCliManager(hub=hub, config=cfg, gateway=_StrictOpenAIGateway(), default_model="flash")
        ok, payload = mgr.chat("查一下在线节点", model_name="flash")
        assert ok is True
        assert payload["reply"] == "已查询到在线节点。"
        assert len(seen_rounds) == 2

        # 同时验证 Gemini 工具调用的 provider_metadata (如 thought_signature) 在第 2 轮被完整保留，且空 metadata 不污染 OpenAI 请求
        assert "provider_metadata" not in seen_rounds[1][2]["tool_calls"][0]

        seen_gemini_rounds = []

        class _StrictGeminiGateway:
            def invoke_sync(self, messages, **kwargs):
                seen_gemini_rounds.append([dict(m) for m in messages])
                if len(seen_gemini_rounds) == 1:
                    return {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_gem_1",
                                "name": "list_nodes",
                                "arguments": "{}",
                                "provider_metadata": {"thought_signature": b"sig_xyz"},
                            }
                        ],
                        "usage_total": 20,
                    }
                asst_msg = [m for m in messages if m.get("role") == "assistant"][-1]
                tc0 = asst_msg["tool_calls"][0]
                assert tc0["type"] == "function"
                assert tc0["provider_metadata"] == {"thought_signature": b"sig_xyz"}
                return {"content": "Gemini 第二轮完成", "tool_calls": [], "usage_total": 15}

        mgr_gem = PcCliManager(hub=hub, config=cfg, gateway=_StrictGeminiGateway(), default_model="flash")
        ok_g, payload_g = mgr_gem.chat("Gemini 查节点", model_name="flash")
        assert ok_g is True
        assert payload_g["reply"] == "Gemini 第二轮完成"
    finally:
        hub.close()


def test_manager_list_node_dirs_tool_and_failed_history_filter(tmp_path):
    """回归：AI 总管支持 list_node_dirs 浏览真实目录；前端过滤发送失败的 managerMessages 不污染后续历史。"""
    from pathlib import Path
    from lite_agent.features.pc_cli.hub import PcCliHub
    from lite_agent.features.pc_cli.manager import MANAGER_TOOLS_SCHEMA, PcCliManager
    from lite_agent.features.pc_cli.repository import PcCliRepository

    tool_names = [t["function"]["name"] for t in MANAGER_TOOLS_SCHEMA]
    assert "list_node_dirs" in tool_names

    repo = PcCliRepository(db_path=str(tmp_path / "pc_cli_dirs.db"))
    hub = PcCliHub(repo=repo)
    try:
        captured = {}

        def _fake_list_node_dirs(node_id, path="", timeout=5.0):
            captured["args"] = (node_id, path)
            if path == "/Users/lilee/nonexistent-bill-query":
                return False, {"error": f"目录不存在: {path}"}
            many = [{"name": f"proj_{i:03d}_{'x' * 120}", "path": f"/Users/lilee/Projects/proj_{i:03d}_{'x' * 120}"} for i in range(90)]
            return True, {"node_id": node_id, "path": path, "entries": many}

        hub.list_node_dirs = _fake_list_node_dirs  # type: ignore[method-assign]
        hub.handle_daemon_hello("c_m", lambda _: None, {"node_id": "mac-mini", "engines": ["codex", "agy"], "workspace_roots": ["/Users/lilee"]})
        mgr = PcCliManager(hub=hub, config={}, gateway=None)

        # 1. 按条目裁剪目录列表且 _serialize_tool_output 始终保持合法 JSON
        from lite_agent.features.pc_cli.manager import _serialize_tool_output
        import json

        res = mgr.execute_tool("list_node_dirs", {"node_id": "mac-mini", "path": "/Users/lilee/Projects", "limit": 60}, [])
        assert res["ok"] is True
        assert captured["args"] == ("mac-mini", "/Users/lilee/Projects")
        assert len(res["entries"]) == 60
        assert res["total_entries"] == 90
        assert res["truncated"] is True
        serialized = _serialize_tool_output(res, max_chars=2000)
        assert len(serialized) <= 2000
        parsed_back = json.loads(serialized)
        assert parsed_back["truncated"] is True
        assert isinstance(parsed_back["entries"], list) and len(parsed_back["entries"]) < 60

        # 1b. 兜底 preview 分支：含大量引号/反斜杠/控制字符的非列表大对象，二次 JSON 转义后仍严格 <= max_chars 且为合法 JSON
        heavy_escape_out = {"ok": False, "error": ('"\\\n\t\x01' * 3000)}
        fallback_ser = _serialize_tool_output(heavy_escape_out, max_chars=8000)
        assert len(fallback_ser) <= 8000
        fallback_parsed = json.loads(fallback_ser)
        assert fallback_parsed["ok"] is False
        assert fallback_parsed["truncated"] is True
        assert len(fallback_parsed["preview"]) > 0

        # 2. [P2] 创建 Bot 前强制校验目标节点目录真实存在：不存在时拒绝入库
        bad_create = mgr.execute_tool(
            "create_bot",
            {"name": "bad-bot", "node_id": "mac-mini", "engine": "codex", "cwd": "/Users/lilee/nonexistent-bill-query"},
            [],
        )
        assert bad_create["ok"] is False
        assert "目录不存在" in bad_create["error"]
        assert hub.list_bots() == []

        ok_create = mgr.execute_tool(
            "create_bot",
            {"name": "good-bot", "node_id": "mac-mini", "engine": "codex", "cwd": "/Users/lilee/Projects"},
            [],
        )
        assert ok_create["ok"] is True
        assert len(hub.list_bots()) == 1
    finally:
        hub.close()

    root = Path(__file__).resolve().parents[3]
    app_js = (root / "web_dashboard" / "mobile" / "app.js").read_text(encoding="utf-8")
    store_js = (root / "web_dashboard" / "mobile" / "store.js").read_text(encoding="utf-8")
    index_html = (root / "web_dashboard" / "mobile" / "index.html").read_text(encoding="utf-8")
    assert "userEntry.failed = true" in app_js
    assert "!m.failed" in app_js
    assert "!m.failed" in store_js
    assert "export async function updateBot(" in store_js
    assert "method: 'PATCH'" in store_js
    assert "handleToggleApprovalMode" in app_js
    assert "sending.value || updatingApprovalMode.value" in app_js
    assert "sending || updatingApprovalMode || !promptInput.trim()" in index_html
    assert "handleToggleApprovalMode(activeBot)" in index_html
    style_css = (root / "web_dashboard" / "mobile" / "style.css").read_text(encoding="utf-8")
    assert 'class="approval-stack"' in index_html
    assert 'class="approval-details"' in index_html
    assert "formatApprovalFullDetail(appr)" in index_html
    assert "function formatApprovalFullDetail(appr)" in app_js
    assert "networkApprovalContext" in app_js
    assert "additionalPermissions" in app_js
    assert "min-height: 0;" in style_css


def test_model_discovery_and_execution_mode_reasoning_effort(tmp_path):
    """验证 Codex 本地模型发现、Hub 持久化/校验、Codex/AGY 适配器参数与移动端契约。"""
    from lite_agent.features.pc_cli.daemon.adapters.agy_adapter import AgyCliAdapter
    from lite_agent.features.pc_cli.daemon.adapters.base import apply_execution_mode_prompt
    from lite_agent.features.pc_cli.daemon.adapters.codex_adapter import CodexCliAdapter
    from lite_agent.features.pc_cli.daemon.env_probe import discover_codex_models

    # 1. Codex 本地模型发现（过滤 hide、识别默认模型与 reasoning_efforts、拦截非法 slug）
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text('model = "gpt-6-luna"\n[projects."/a"]\nmodel = "ignored"\n', encoding="utf-8")
    (codex_dir / "models_cache.json").write_text(
        json.dumps({
            "models": [
                {
                    "slug": "gpt-6-luna",
                    "display_name": "GPT-6 Luna",
                    "visibility": "list",
                    "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
                },
                {"slug": "gpt-reserve", "display_name": "Hidden", "visibility": "hide"},
                {"slug": "bad model;rm", "display_name": "Bad", "visibility": "list"},
                {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list"},
            ]
        }),
        encoding="utf-8",
    )
    found = discover_codex_models(codex_home=str(codex_dir))
    assert [m["id"] for m in found] == ["gpt-6-luna", "gpt-6.1-sol"]
    assert found[0]["is_default"] is True
    assert found[0]["reasoning_efforts"] == ["low", "high"]
    assert found[1]["is_default"] is False
    assert discover_codex_models(codex_home=str(tmp_path / "missing")) == []

    # 2. Hub 持久化与 turn.start 透传 execution_mode / reasoning_effort / engine_models
    sent_to_daemon: list[dict] = []
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        node = hub.handle_daemon_hello(
            "c1",
            lambda msg: sent_to_daemon.append(msg),
            {
                "node_id": "win1",
                "engines": ["codex", "agy"],
                "engine_models": {"codex": found, "agy": [], "unknown": [{"id": "x"}]},
                "workspace_roots": ["/w"],
            },
        )
        assert "unknown" not in node["engine_models"]
        assert [m["id"] for m in node["engine_models"]["codex"]] == ["gpt-6-luna", "gpt-6.1-sol"]

        bad_em, _ = hub.create_bot(
            name="b1", node_id="win1", engine="codex", cwd="/w", execution_mode="invalid"
        )
        assert bad_em is False
        bad_re, _ = hub.create_bot(
            name="b1", node_id="win1", engine="codex", cwd="/w", reasoning_effort="ultra"
        )
        assert bad_re is False

        ok_b, b_res = hub.create_bot(
            name="planner",
            node_id="win1",
            engine="codex",
            cwd="/w",
            execution_mode="plan",
            reasoning_effort="high",
            model="gpt-6-luna",
        )
        assert ok_b is True
        bot = b_res["bot"]
        assert bot["execution_mode"] == "plan"
        assert bot["reasoning_effort"] == "high"
        assert bot["model"] == "gpt-6-luna"

        ok_u, u_res = hub.update_bot(bot["bot_id"], execution_mode="chat", reasoning_effort="medium")
        assert ok_u is True
        assert u_res["bot"]["execution_mode"] == "chat"
        assert u_res["bot"]["reasoning_effort"] == "medium"

        ok_t, _ = hub.start_turn(bot["bot_id"], "解释架构")
        assert ok_t is True
        assert sent_to_daemon[-1]["execution_mode"] == "chat"
        assert sent_to_daemon[-1]["reasoning_effort"] == "medium"
        assert sent_to_daemon[-1]["model"] == "gpt-6-luna"
    finally:
        hub.close()

    # 3. 适配器参数与统一提示词抽象
    codex = CodexCliAdapter(executable="codex", env={})
    c_args = codex.build_args(
        native_session_id=None, approval_mode="auto", model="gpt-6-luna",
        execution_mode="plan", reasoning_effort="high",
    )
    assert 'sandbox_mode="read-only"' in c_args
    assert 'model_reasoning_effort="high"' in c_args
    assert 'sandbox_mode="workspace-write"' not in c_args
    c_app_args = codex.build_app_server_args(reasoning_effort="xhigh")
    assert 'model_reasoning_effort="xhigh"' in c_app_args

    agy = AgyCliAdapter(executable="agy", env={})
    a_agent = agy.build_args(cwd="/w", approval_mode="auto", execution_mode="agent")
    a_plan = agy.build_args(cwd="/w", approval_mode="auto", execution_mode="plan")
    assert "--dangerously-skip-permissions" in a_agent
    assert "--dangerously-skip-permissions" not in a_plan

    assert apply_execution_mode_prompt("hello", "agent", "") == "hello"
    plan_p = apply_execution_mode_prompt("hello", "plan", "high", include_effort_hint=True)
    assert "[模式: PLAN" in plan_p and "[思考深度: high" in plan_p and plan_p.endswith("hello")

    # 4. 移动端前端契约
    root = Path(__file__).resolve().parents[3]
    app_js = (root / "web_dashboard" / "mobile" / "app.js").read_text(encoding="utf-8")
    index_html = (root / "web_dashboard" / "mobile" / "index.html").read_text(encoding="utf-8")
    assert "selectedEngineModels" in app_js and "selectedEngineModels" in index_html
    assert "handleCycleExecutionMode" in app_js and "handleCycleExecutionMode(activeBot)" in index_html
    assert "handleCycleReasoningEffort" in app_js and "handleCycleReasoningEffort(activeBot)" in index_html
    assert "form.execution_mode" in index_html and "form.reasoning_effort" in index_html


def test_readonly_escalation_bounds_and_agy_acp_model(tmp_path):
    """回归验证 3 项 P2：Codex 只读模式锁定 approvalPolicy=never + 拒绝提权、AGY 只读模式拒绝自动放行、AGY ACP 透传 session/set_model。"""
    import asyncio
    import os
    import sys
    from lite_agent.features.pc_cli.daemon.adapters.agy_adapter import AgyCliAdapter
    from lite_agent.features.pc_cli.daemon.adapters.codex_adapter import CodexCliAdapter

    # 1. Codex app-server: plan + ask -> 必须传 approvalPolicy="never" + sandbox="read-only"，且拒绝服务端提权请求
    codex_script = tmp_path / "fake_codex_ro.py"
    codex_script.write_text(
        """import json, sys
def send(obj):
    print(json.dumps(obj), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if "method" in msg and "id" in msg:
        method = msg["method"]
        if method == "initialize":
            send({"id": msg["id"], "result": {}})
        elif method == "thread/start":
            p = msg["params"]
            assert p["sandbox"] == "read-only", f"bad sandbox: {p}"
            assert p["approvalPolicy"] == "never", f"bad policy: {p}"
            send({"id": msg["id"], "result": {"thread": {"id": "th-ro-1"}}})
        elif method == "turn/start":
            send({"id": msg["id"], "result": {"turn": {"id": "t-ro-1"}}})
            send({"id": 99, "method": "item/fileChange/requestApproval", "params": {"reason": "write"}})
    elif msg.get("id") == 99 and "result" in msg:
        dec = msg["result"].get("decision")
        send({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": f"DECISION:{dec}"}}})
        send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
""",
        encoding="utf-8",
    )

    class _FakeCodexRo(CodexCliAdapter):
        def build_app_server_args(self, *, reasoning_effort: str = ""):
            return [sys.executable, str(codex_script)]

    approval_called = []

    async def _never_call(_req):
        approval_called.append(_req)
        return "allow_once"

    res_c = asyncio.run(
        _FakeCodexRo(env=dict(os.environ), turn_timeout_sec=10).run_turn(
            bot_id="b1",
            turn_id="t1",
            cwd=str(tmp_path),
            prompt="plan refactor",
            native_session_id=None,
            approval_mode="ask",
            model="gpt-6-luna",
            execution_mode="plan",
            reasoning_effort="high",
            on_event=lambda *_: asyncio.sleep(0),
            on_approval=_never_call,
        )
    )
    assert res_c.status == "completed"
    assert res_c.final_text == "DECISION:decline"
    assert approval_called == []

    # 2. AGY: plan + auto -> 走 ACP，透传 session/set_model，且对 session/request_permission 强制 reject_once
    agy_script = tmp_path / "fake_agy_ro.py"
    agy_script.write_text(
        """import json, sys
def send(obj):
    print(json.dumps(obj), flush=True)
seen_model = None
pending_prompt_id = None
for line in sys.stdin:
    msg = json.loads(line)
    if "method" in msg and "id" in msg:
        method = msg["method"]
        if method == "initialize":
            send({"id": msg["id"], "result": {}})
        elif method == "session/new":
            send({"id": msg["id"], "result": {"sessionId": "11111111-2222-3333-4444-555555555555"}})
        elif method == "session/set_model":
            seen_model = msg["params"].get("modelId")
            send({"id": msg["id"], "result": {}})
        elif method == "session/prompt":
            pending_prompt_id = msg["id"]
            send({
                "id": 77,
                "method": "session/request_permission",
                "params": {
                    "toolCall": {"title": "write_file", "rawInput": "rm -rf"},
                    "options": [
                        {"kind": "allow_once", "optionId": "opt_allow"},
                        {"kind": "reject_once", "optionId": "opt_reject"},
                    ],
                },
            })
    elif msg.get("id") == 77 and "result" in msg:
        opt = msg["result"]["outcome"]["optionId"]
        send({
            "method": "session/update",
            "params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": f"MODEL:{seen_model}|OPT:{opt}"}}},
        })
        send({"id": pending_prompt_id, "result": {"stopReason": "end_turn"}})
""",
        encoding="utf-8",
    )

    class _FakeAgyRo(AgyCliAdapter):
        def build_acp_args(self, *, cwd: str, approval_mode: str, execution_mode: str = "agent"):
            return [sys.executable, str(agy_script)]

    res_a = asyncio.run(
        _FakeAgyRo(env=dict(os.environ), turn_timeout_sec=10).run_turn(
            bot_id="b2",
            turn_id="t2",
            cwd=str(tmp_path),
            prompt="plan only",
            native_session_id=None,
            approval_mode="auto",
            model="gemini-3-pro",
            execution_mode="plan",
            reasoning_effort="high",
            on_event=lambda *_: asyncio.sleep(0),
            on_approval=_never_call,
        )
    )
    assert res_a.status == "completed"
    assert res_a.final_text == "MODEL:gemini-3-pro|OPT:opt_reject"
    assert approval_called == []

    # 3. AGY ACP: session/set_model 返回错误时发出非致命 warning 事件而非静默忽略
    agy_warn_script = tmp_path / "fake_agy_warn.py"
    agy_warn_script.write_text(
        """import json, sys
def send(obj):
    print(json.dumps(obj), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    if "method" in msg and "id" in msg:
        method = msg["method"]
        if method == "initialize":
            send({"id": msg["id"], "result": {}})
        elif method == "session/new":
            send({"id": msg["id"], "result": {"sessionId": "22222222-2222-3333-4444-555555555555"}})
        elif method == "session/set_model":
            send({"id": msg["id"], "error": {"code": -32601, "message": "Method not found"}})
        elif method == "session/prompt":
            send({"method": "session/update", "params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "OK"}}}})
            send({"id": msg["id"], "result": {"stopReason": "end_turn"}})
""",
        encoding="utf-8",
    )

    class _FakeAgyWarn(AgyCliAdapter):
        def build_acp_args(self, *, cwd: str, approval_mode: str, execution_mode: str = "agent"):
            return [sys.executable, str(agy_warn_script)]

    captured_events = []

    async def _capture(etype, payload):
        captured_events.append((etype, payload))

    res_w = asyncio.run(
        _FakeAgyWarn(env=dict(os.environ), turn_timeout_sec=10, prefer_acp=True).run_turn(
            bot_id="b3",
            turn_id="t3",
            cwd=str(tmp_path),
            prompt="hi",
            native_session_id=None,
            approval_mode="auto",
            model="custom-model",
            execution_mode="agent",
            on_event=_capture,
        )
    )
    assert res_w.status == "completed"
    assert any(et == "warning" and "custom-model" in str(p.get("message", "")) for et, p in captured_events)


def test_reset_session_and_mobile_markdown_upgrade():
    """验证开启新会话（reset_session）的运行中保护与清空 native_session_id，以及移动端 GFM Markdown 契约。"""
    repo = PcCliRepository(":memory:")
    hub = PcCliHub(repo=repo)
    try:
        hub.handle_daemon_hello("c1", lambda _: None, {"node_id": "mac1", "engines": ["codex"], "workspace_roots": ["/w"]})
        ok_b, b_res = hub.create_bot(name="demo", node_id="mac1", engine="codex", cwd="/w")
        assert ok_b is True
        bot_id = b_res["bot"]["bot_id"]

        # 模拟已有历史会话 ID
        repo.set_bot_status(bot_id, "idle", native_session_id="thread_old_123")
        assert repo.get_bot(bot_id)["native_session_id"] == "thread_old_123"

        # 运行中禁止重置会话
        ok_t, t_res = hub.start_turn(bot_id, "working")
        assert ok_t is True
        ok_busy, err_busy = hub.update_bot(bot_id, reset_session=True)
        assert ok_busy is False
        assert "运行中" in err_busy["error"]
        assert repo.get_bot(bot_id)["native_session_id"] == "thread_old_123"

        # 回合结束后允许重置，native_session_id 置空
        hub.handle_daemon_turn_finished(
            {"bot_id": bot_id, "turn_id": t_res["turn"]["turn_id"], "status": "completed", "native_session_id": "thread_old_123"},
            sender_node_id="mac1",
            conn_id="c1",
        )
        ok_reset, res_reset = hub.update_bot(bot_id, reset_session=True)
        assert ok_reset is True
        assert res_reset["bot"]["native_session_id"] is None
    finally:
        hub.close()

    app_js = (MOBILE_DIR / "app.js").read_text(encoding="utf-8")
    index_html = (MOBILE_DIR / "index.html").read_text(encoding="utf-8")
    style_css = (MOBILE_DIR / "style.css").read_text(encoding="utf-8")
    assert "getSafeMarkdownRenderer" in app_js
    assert "md-table-wrap" in app_js and ".md-table-wrap" in style_css
    assert "md-file-ref" in app_js and "code.md-file-ref" in style_css
    assert "handleResetSession" in app_js and "handleResetSession(activeBot)" in index_html
    assert 'class="msg-assistant is-streaming"' in index_html
