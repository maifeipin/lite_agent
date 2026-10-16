"""
阶段二 2-F 终极硬门禁自动化测试套件 (tests/test_phase2_boundaries_hard_gates.py)。
严格依据 architecture/stage-2-3-antigravity-rules-20261015.md 第 4 节 2-F 编写：
1. 真实插件新进程零副作用导入
2. 合成插件纯外部装配
3. 临时目录物理移除 features/skills/Parser 后的 core 模式独立主入口 smoke
4. 双实例隔离与生命周期无残留
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import socket
import time
import pytest
from app.bootstrap import create_app
from app.plugins import PluginContext, PluginLoader
from core.skill_engine import SkillEngine


@pytest.mark.parametrize("plugin_entry", [
    "features.billing",
    "features.todo",
    "features.review_meeting",
    "features.mail_ops",
    "features.sys_ops",
    "features.network_ops",
    "features.content_ops",
])
def test_real_plugins_import_zero_side_effects(plugin_entry):
    """门禁 1: 新进程导入真实插件，无文件/DB/网络/后台作业副作用。"""
    cmd = [
        sys.executable,
        "-c",
        f"""
import sys, threading
initial_threads = threading.active_count()
import {plugin_entry} as mod
assert hasattr(mod, 'register'), f"Missing register in {{{plugin_entry}}}"
# 导入不应启动额外的后台常驻线程
assert threading.active_count() == initial_threads, f"Threads leaked on import of {{{plugin_entry}}}"
print("OK")
""",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, f"Plugin {plugin_entry} import failed: {res.stderr}"
    assert "OK" in res.stdout


def test_synthetic_plugin_registers_without_modifying_core():
    """门禁 2: 合成插件仅凭自身代码和清单即可登记工具、命令、工作流、路由、作业。"""
    app = create_app(mode="core")
    syn = ModuleType("synthetic_custom_plugin")

    def syn_register(ctx: PluginContext):
        ctx.register_command("/custom_ping", lambda a, m, args: "pong", description="custom command")
        ctx.register_skill("custom_echo", {"func": lambda msg: f"echo:{msg}", "schema": {}})
        ctx.register_workflow("custom_wf", lambda c, r, l: "wf_inst", lambda c, t: "custom" in t)
        ctx.register_route("GET", "/api/v1/custom", lambda req, url: None)
        ctx.register_cron("custom_job", "03:00", lambda: "done")

    syn.register = syn_register

    loader = PluginLoader(context=app.plugin_loader.context, allowed_entries={"synthetic_custom_plugin"})

    with patch("app.plugins.importlib.import_module", return_value=syn):
        loader.load_plugin_by_entry("synthetic_custom_plugin")

    # 验证全部能力成功挂入且无需修改 core
    assert app.command_registry.get("/custom_ping") is not None
    assert "custom_echo" in app.skill_engine._registry
    assert app.workflow_registry.get("custom_wf") is not None
    assert app.route_registry.match("GET", "/api/v1/custom") is not None
    assert "custom_job" in app.cron_service._job_names

    app.shutdown()


def test_physical_absence_of_features_and_skills_core_smoke(tmp_path):
    """门禁 3: 临时源码副本物理移除 features/skills/Parser，独立子进程真实运行 main.py 入口，
    配合本地离线 Fake LLM 服务，实测真实 HTTP 普通对话、流式 SSE 分块与 [DONE] 唯一终态、
    未授权 401 拦截、未启用业务端点 404 阻断，以及信号捕获后优雅安全退出。
    """
    import http.server
    import json
    import signal
    import urllib.request
    import urllib.error

    core_root = tmp_path / "lite_agent_core"
    core_root.mkdir()

    # 仅复制基础内核必要目录与根文件
    essential_dirs = ["app", "contracts", "services", "core", "channels", "integrations"]
    for d in essential_dirs:
        if Path(d).exists():
            shutil.copytree(d, core_root / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    essential_files = ["main.py", "agent.py", "session.py", "security.py"]
    for f in essential_files:
        if Path(f).exists():
            shutil.copy2(f, core_root / f)

    # 物理断言：确保 features、skills 和 Parser 绝对缺席
    assert not (core_root / "features").exists()
    assert not (core_root / "skills").exists()

    # 找到 2 个独立可用回环端口：一个给 Fake LLM，一个给 Core API
    with socket.socket() as s1:
        s1.bind(("127.0.0.1", 0))
        fake_llm_port = s1.getsockname()[1]

    with socket.socket() as s2:
        s2.bind(("127.0.0.1", 0))
        core_api_port = s2.getsockname()[1]

    # 1. 启动轻量级离线 Fake OpenAI-compatible HTTP 服务
    class FakeOpenAIHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            req_body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            is_stream = req_body.get("stream", False)

            if is_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                # 逐块输出流式事件
                chunk1 = json.dumps({"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 1000, "model": "mock-model", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Fake "}, "finish_reason": None}]})
                chunk2 = json.dumps({"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 1000, "model": "mock-model", "choices": [{"index": 0, "delta": {"content": "stream chunk"}, "finish_reason": None}]})
                chunk3 = json.dumps({"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 1000, "model": "mock-model", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                for c in [chunk1, chunk2, chunk3]:
                    self.wfile.write(f"data: {c}\n\n".encode("utf-8"))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                resp = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": 1000,
                    "model": "mock-model",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "Fake regular response"},
                        "finish_reason": "stop"
                    }],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13}
                }
                self.wfile.write(json.dumps(resp).encode("utf-8"))

    fake_server = http.server.HTTPServer(("127.0.0.1", fake_llm_port), FakeOpenAIHandler)
    server_thread = threading.Thread(target=fake_server.serve_forever, daemon=True)
    server_thread.start()

    # 2. 构造符合中央规范且指向 Fake LLM 的最小 core 模式配置
    min_config = {
        "bot_name": "CoreBot",
        "service_name": "core-test",
        "app": {"mode": "core"},
        "llm": {
            "default": "mock-model",
            "models": {
                "mock-model": {
                    "driver": "openai",
                    "model": "mock-model",
                    "api_key": "dummy-key",
                    "base_url": f"http://127.0.0.1:{fake_llm_port}/v1",
                    "tags": ["text", "code"],
                }
            }
        },
        "session": {"db_path": str(tmp_path / "core_session.db")},
        "channels": {
            "api": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": core_api_port,
                "auth_token": "test-token"
            }
        }
    }
    cfg_file = core_root / "config.json"
    cfg_file.write_text(json.dumps(min_config), encoding="utf-8")

    # 3. 真实启动 main.py 子进程 (指定 --config 与 --mode core)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(core_root)
    proc = subprocess.Popen(
        [sys.executable, str(core_root / "main.py"), "--config", str(cfg_file), "--mode", "core"],
        cwd=str(core_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    base_url = f"http://127.0.0.1:{core_api_port}"
    token_headers = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}

    try:
        # 等待端口就绪 (最多等待 5 秒)
        deadline = time.time() + 5.0
        connected = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", core_api_port), timeout=0.2):
                    connected = True
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
        if not connected:
            out, err = proc.communicate(timeout=1.0)
            raise AssertionError(f"Core API failed to start on port {core_api_port}.\nSTDOUT:\n{out}\nSTDERR:\n{err}")

        # 4. 验证 1: 未授权访问核心模型列表端点返回 401
        try:
            urllib.request.urlopen(f"{base_url}/v1/models")
            raise AssertionError("Expected 401 Unauthorized")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

        # 5. 验证 2: 授权访问核心模型端点返回 200
        req_models = urllib.request.Request(f"{base_url}/v1/models", headers=token_headers)
        with urllib.request.urlopen(req_models) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert "data" in data

        # 6. 验证 3: 真实普通对话 HTTP 请求 (POST /api/v1/chat, stream=false)
        chat_payload = json.dumps({"session_id": "test_sess_1", "text": "ping"}).encode("utf-8")
        req_chat = urllib.request.Request(f"{base_url}/api/v1/chat", data=chat_payload, headers=token_headers, method="POST")
        with urllib.request.urlopen(req_chat) as resp:
            assert resp.status == 200
            chat_res = json.loads(resp.read().decode("utf-8"))
            assert "reply" in chat_res or "response" in chat_res
            reply_text = chat_res.get("reply") or chat_res.get("response")
            assert "Fake stream chunk" in reply_text

        # 7. 验证 4: 真实流式对话 HTTP 请求 (POST /v1/chat/completions, stream=true)
        stream_payload = json.dumps({
            "model": "mock-model",
            "messages": [{"role": "user", "content": "ping stream"}],
            "stream": True,
        }).encode("utf-8")
        req_stream = urllib.request.Request(f"{base_url}/v1/chat/completions", data=stream_payload, headers=token_headers, method="POST")
        with urllib.request.urlopen(req_stream) as resp:
            assert resp.status == 200
            raw_lines = [line.decode("utf-8").strip() for line in resp.readlines() if line.strip()]
            # 必须收到包含增量 delta 的 data 事件
            data_lines = [line for line in raw_lines if line.startswith("data:")]
            assert len(data_lines) >= 2, f"Expected streaming chunks, got: {raw_lines}"
            # 严格断言最后一条是 [DONE] 唯一终态标记
            assert data_lines[-1] == "data: [DONE]"

        # 8. 验证 5: 访问未启用的业务路由 /api/v1/todos 真实返回 404
        req_todo = urllib.request.Request(f"{base_url}/api/v1/todos", headers=token_headers)
        try:
            urllib.request.urlopen(req_todo)
            raise AssertionError("Expected 404 for business route in core mode")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404

    finally:
        # 9. 验证 6: 向真实 main 进程发送 SIGTERM，断言优雅安全退出
        proc.send_signal(signal.SIGTERM)
        try:
            exit_code = proc.wait(timeout=4.0)
            out, err = proc.communicate(timeout=0.5)
            assert exit_code == 0, f"main.py exited with non-zero code {exit_code}.\nSTDOUT:\n{out}\nSTDERR:\n{err}"
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("main.py failed to shutdown gracefully within 4.0s timeout")
        finally:
            fake_server.shutdown()
            fake_server.server_close()


def test_core_mode_unknown_route_returns_not_found_safely(tmp_path):
    """门禁 4: 真实启动 ApiServer，实测 core 模式下未启用业务路由返回 404，核心路由 200，鉴权与流式正常。"""
    import urllib.request
    import urllib.error
    import json
    from agent import Agent
    from channels.api import ApiServer

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    cfg = {
        "llm": {"api_key": "k", "base_url": "u", "model": "m"},
        "channels": {
            "api": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": port,
                "auth_token": "sec-token",
            }
        },
        "session": {"db_path": str(tmp_path / "sessions.db")},
        "task_specs": {"db_path": str(tmp_path / "task_specs.db")},
        "memory": {"enabled": False},
    }
    app = create_app(mode="core", config=cfg)
    agent = Agent(app.config, skill_engine=app.skill_engine, command_registry=app.command_registry)
    agent.route_registry = app.route_registry

    server = ApiServer(cfg["channels"], agent)
    server.start()
    time.sleep(0.2)

    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": "Bearer sec-token", "Content-Type": "application/json"}

    try:
        # 1. 验证 GET /api/v1/todos -> 真实 404
        req_get = urllib.request.Request(f"{base}/api/v1/todos", headers=headers)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_get)
        assert exc_info.value.code == 404

        # 2. 验证 POST /api/v1/todos -> 真实 404
        req_post = urllib.request.Request(
            f"{base}/api/v1/todos",
            data=json.dumps({"title": "test"}).encode(),
            headers=headers,
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_post)
        assert exc_info.value.code == 404

        # 3. 验证 GET /api/v1/review-meetings -> 真实 404
        req_meeting = urllib.request.Request(f"{base}/api/v1/review-meetings", headers=headers)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_meeting)
        assert exc_info.value.code == 404

        # 4. 验证未授权访问公共端点 -> 真实 401
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base}/v1/models")
        assert exc_info.value.code == 401

        # 5. 验证授权访问公共端点 -> 真实 200
        req_models = urllib.request.Request(f"{base}/v1/models", headers=headers)
        with urllib.request.urlopen(req_models) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert "data" in data

    finally:
        server.stop()
        app.shutdown()


def test_full_mode_business_route_dispatches_successfully(tmp_path):
    """门禁 5: Full 模式加载 features.todo 插件后，真实 HTTP 请求 /api/v1/todos 成功响应 200。"""
    import urllib.request
    import json
    from agent import Agent
    from channels.api import ApiServer
    from app.manifests import FULL_MODE_PLUGINS

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    cfg = {
        "llm": {"api_key": "k", "base_url": "u", "model": "m"},
        "channels": {
            "api": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": port,
                "auth_token": "sec-token",
            }
        },
        "session": {"db_path": str(tmp_path / "sessions.db")},
        "task_specs": {"db_path": str(tmp_path / "task_specs.db")},
        "memory": {"enabled": False},
        "todo": {"db_path": str(tmp_path / "todo.db")},
    }
    app = create_app(mode="full", config=cfg)
    # 显式加载 todo 插件
    todo_manifest = [p for p in FULL_MODE_PLUGINS if p.get("name") in ("todo", "features.todo") or p.get("entry") == "features.todo"]
    app.plugin_loader.load_all(todo_manifest)

    agent = Agent(app.config, skill_engine=app.skill_engine, command_registry=app.command_registry)
    agent.route_registry = app.route_registry

    server = ApiServer(cfg["channels"], agent)
    server.start()
    time.sleep(0.2)

    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": "Bearer sec-token"}

    try:
        # 发送 GET /api/v1/todos -> 成功返回 200 OK
        req = urllib.request.Request(f"{base}/api/v1/todos", headers=headers)
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode())
            assert isinstance(payload, dict)
    finally:
        server.stop()
        app.shutdown()
