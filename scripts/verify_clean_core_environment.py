#!/usr/bin/env python3
"""
干净环境独立运行证明脚本 (scripts/verify_clean_core_environment.py)
阶段三 S3-A2 核心验收工具。

从仓外 cwd、独立虚拟环境 (venv)、独立进程、干净依赖仅加载 core 基础源码副本：
1. 创建仓外独立 venv (--system-site-packages 关闭)，仅安装 requirements-core.txt；
2. 收集并记录解释器元数据、sys.path、包清单、所有加载模块 __file__ 及其来源分类；
3. 断言 features、skills、Parser 模块完全缺席 (不可导入且未进入 sys.modules)；
4. 断言 memory_engine 缺包时平滑降级启动 (MEMORY_AVAILABLE=False)；
5. 启动同一 main.py 独立子进程与公共 ApiServer 宿主；
6. 在本地回环网络执行普通对话、流式对话 (SSE 完整事件解析与单次 [DONE] 断言)、鉴权认证、健康状态检查及未启用业务 404 验证；
7. 验证 SIGINT 优雅退出与真实 returncode == 0。
"""

import argparse
import importlib.metadata
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import venv
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, Tuple, Optional, List


class MockLLMHandler(BaseHTTPRequestHandler):
    """本地回环假模型服务，支持 OpenAI 兼容的同步与流式响应。"""

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        req = json.loads(body.decode("utf-8")) if body else {}
        is_stream = req.get("stream", False)

        if is_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            chunks = ["Hello", " from", " clean", " core", " stream!"]
            for c in chunks:
                chunk_obj = {
                    "id": "chatcmpl-mock-stream",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "mock-model-v1",
                    "choices": [{"index": 0, "delta": {"content": c}, "finish_reason": None}],
                }
                self.wfile.write(f"data: {json.dumps(chunk_obj)}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.005)
            final_obj = {
                "id": "chatcmpl-mock-stream",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "mock-model-v1",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            self.wfile.write(f"data: {json.dumps(final_obj)}\n\ndata: [DONE]\n\n".encode("utf-8"))
            self.wfile.flush()
        else:
            resp_data = {
                "id": "chatcmpl-mock-sync",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "mock-model-v1",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Hello from clean core mock server!",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 8,
                    "total_tokens": 18,
                },
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp_data).encode("utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Lite Agent 干净环境独立运行证明工具 (Stage 3 S3-A2)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--agent-root",
        type=str,
        default=None,
        help="Agent 仓库根路径 (默认自动推导)",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="验证完成后保留仓外临时目录用于调试",
    )
    parser.add_argument(
        "--find-links",
        type=str,
        default=None,
        help="可选显式离线 wheelhouse 或 cache 目录",
    )
    parser.add_argument("--report-json", help="保存可复核的解释器、包、模块来源与步骤证据")
    return parser.parse_args()


def run_clean_core_verification(
    agent_root: str,
    keep_temp: bool = False,
    find_links: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any]]:
    report: Dict[str, Any] = {
        "steps": [],
        "passed": False,
        "interpreter_meta": {},
        "packages": [],
        "sys_path": [],
        "module_files": {},
    }

    real_agent_root = os.path.realpath(agent_root)
    export_script = os.path.join(agent_root, "scripts", "export_core.py")
    manifest_path = os.path.join(agent_root, "architecture", "kernel-core-manifest.txt")
    req_core_path = os.path.join(agent_root, "requirements-core.txt")

    tmp_dir = tempfile.mkdtemp(prefix="clean_core_standalone_")
    real_tmp_dir = os.path.realpath(tmp_dir)
    print(f"📁 创建仓外独立运行目录: {real_tmp_dir}")

    mock_llm_server = None
    proc = None

    try:
        # Step 1: 启动本地回环 Mock LLM 服务 (零外部联网依赖)
        mock_llm_server = HTTPServer(("127.0.0.1", 0), MockLLMHandler)
        mock_llm_port = mock_llm_server.server_address[1]
        threading.Thread(target=mock_llm_server.serve_forever, daemon=True, name="MockLLMServer").start()
        print(f"🤖 本地回环假模型服务启动于: http://127.0.0.1:{mock_llm_port}")
        report["steps"].append({"name": "mock_llm_start", "passed": True, "port": mock_llm_port})

        # Step 2: 导出纯净 core 副本至仓外 cwd
        exp_res = subprocess.run(
            [
                sys.executable,
                export_script,
                "--manifest",
                manifest_path,
                "--agent-root",
                agent_root,
                "--output-dir",
                tmp_dir,
                "--check-hashes",
            ],
            capture_output=True,
            text=True,
        )
        if exp_res.returncode != 0:
            print(f"❌ 导出 core 副本失败: {exp_res.stderr}")
            report["steps"].append({"name": "export_core", "passed": False, "error": exp_res.stderr})
            return False, report

        print("✅ core 基础源码生成物导出成功")
        report["steps"].append({"name": "export_core", "passed": True})

        # Step 3: 创建不包含 system-site-packages 的干净虚拟环境并安装基础 requirements
        venv_dir = os.path.join(tmp_dir, "venv")
        real_venv_dir = os.path.realpath(venv_dir)
        print(f"🔨 正在创建隔离虚拟环境: {real_venv_dir}")
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(venv_dir)

        venv_python = os.path.join(venv_dir, "bin", "python")
        venv_pip = os.path.join(venv_dir, "bin", "pip")

        if not os.path.isfile(venv_python):
            raise RuntimeError(f"虚拟环境 Python 解释器未生成: {venv_python}")

        # 使用该 venv 的 pip 安装仅限 requirements-core.txt 的依赖
        find_links = find_links or os.environ.get("PIP_FIND_LINKS")
        pip_install_cmd = [venv_pip, "install", "--no-input"]
        if find_links and os.path.isdir(find_links):
            pip_install_cmd.extend(["--find-links", find_links])
            if os.environ.get("PIP_NO_INDEX") in ("1", "true", "True"):
                pip_install_cmd.append("--no-index")
        pip_install_cmd.extend(["-r", req_core_path])

        pip_res = subprocess.run(
            pip_install_cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        report["dependency_install"] = {
            "command": pip_install_cmd,
            "exit_code": pip_res.returncode,
            "stdout": pip_res.stdout,
            "stderr": pip_res.stderr,
        }
        if pip_res.returncode != 0:
            print(f"❌ 虚拟环境安装基础依赖失败 (exit={pip_res.returncode}):\n{pip_res.stderr}")
            report["steps"].append({
                "name": "venv_install_requirements",
                "passed": False,
                "error": pip_res.stderr,
            })
            return False, report

        print("✅ 隔离虚拟环境依赖安装成功 (仅限 requirements-core.txt)")
        report["steps"].append({"name": "venv_install_requirements", "passed": True})

        # Step 4: 收集解释器元数据、已安装包清单、探测全量已加载模块来源及业务缺席断言
        probe_code = f"""
import sys
import os
import json
import importlib
import importlib.metadata

tmp_dir = os.path.realpath({repr(tmp_dir)})
agent_root = os.path.realpath({repr(agent_root)})
venv_dir = os.path.realpath({repr(venv_dir)})
if os.path.realpath(sys.prefix) != venv_dir or sys.prefix == sys.base_prefix:
    raise AssertionError("验收解释器未处于独立虚拟环境")

# 1. 严格检查 sys.path: 严禁包含原仓库目录与外部全局路径
for p in sys.path:
    if not p:
        continue
    real_p = os.path.realpath(p)
    if os.path.commonpath([agent_root, real_p]) == agent_root:
        print(f"SECURITY ERROR: 原仓库目录泄漏在 sys.path: {{p}}", file=sys.stderr)
        sys.exit(10)
    if "site-packages" in real_p.split(os.sep) and os.path.commonpath([venv_dir, real_p]) != venv_dir:
        raise AssertionError("全局 site-packages 泄漏: " + real_p)

# 2. 断言业务模块与外部 Parser 完全缺席 (ModuleNotFoundError)
for forbidden in ["features", "skills", "parser_paths", "integrations.mail", "memory_engine"]:
    try:
        __import__(forbidden)
        print(f"SECURITY ERROR: 业务/可选模块 {{forbidden}} 意外可导入!", file=sys.stderr)
        sys.exit(12)
    except ModuleNotFoundError:
        pass

# 3. 导入核心运行架构模块并核对全量已加载模块来源
import agent
import app.bootstrap
import core.agent_runtime
import channels.api

# 4. 断言 memory_engine 缺包平滑降级
if agent.MEMORY_AVAILABLE:
    print("ERROR: 独立副本中 memory_engine 不应被标记为可用", file=sys.stderr)
    sys.exit(14)

# 5. 扫描 sys.modules 全集分类
categorized = {{}}
for name, mod in list(sys.modules.items()):
    mod_file = getattr(mod, "__file__", None)
    if not mod_file:
        continue
    real_mod_file = os.path.realpath(mod_file)

    # 严禁任何模块来自原仓库
    if os.path.commonpath([agent_root, real_mod_file]) == agent_root:
        print(f"SECURITY ERROR: 模块 {{name}} 来自原仓库目录: {{mod_file}}", file=sys.stderr)
        sys.exit(15)

    if os.path.commonpath([venv_dir, real_mod_file]) == venv_dir:
        cat = "venv_third_party"
    elif os.path.commonpath([tmp_dir, real_mod_file]) == tmp_dir:
        cat = "core_project"
    elif os.path.commonpath([os.path.realpath(sys.base_prefix), real_mod_file]) == os.path.realpath(sys.base_prefix):
        cat = "stdlib"
    else:
        cat = "external_system"
    if cat == "external_system" or (
        "site-packages" in real_mod_file.split(os.sep) and cat != "venv_third_party"
    ):
        raise AssertionError("非虚拟环境模块来源: " + name + ": " + real_mod_file)
    categorized[name] = {{"file": real_mod_file, "category": cat}}

# 验证核心基础模块严格属于 core_project 生成物
for core_pkg in ["agent", "app", "core", "channels", "contracts", "services", "integrations", "security", "session"]:
    for m_name, meta in categorized.items():
        if m_name == core_pkg or m_name.startswith(core_pkg + "."):
            if meta["category"] != "core_project":
                print(f"SECURITY ERROR: 核心模块 {{m_name}} 来源非生成物目录: {{meta['file']}} ({{meta['category']}})", file=sys.stderr)
                sys.exit(16)

# 6. 获取虚拟环境已安装包清单
installed_packages = [
    {{"name": d.metadata["Name"], "version": d.version}}
    for d in importlib.metadata.distributions()
]

print(json.dumps({{
    "status": "ok",
    "interpreter": {{
        "executable": sys.executable,
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
    }},
    "packages": installed_packages,
    "sys_path": sys.path,
    "modules": categorized,
}}))
"""
        clean_env = os.environ.copy()
        for k in list(clean_env.keys()):
            if k.startswith("LITE_AGENT_"):
                clean_env.pop(k, None)
        clean_env.pop("PYTHONPATH", None)
        clean_env.pop("MAIL_STATEMENT_PARSER_DIR", None)

        probe_res = subprocess.run(
            [venv_python, "-c", probe_code],
            cwd=tmp_dir,
            env=clean_env,
            capture_output=True,
            text=True,
        )
        if probe_res.returncode != 0:
            print(f"❌ 仓外隔离断言失败 (exit_code={probe_res.returncode}): {probe_res.stderr}")
            report["steps"].append({"name": "isolated_import_assertions", "passed": False, "error": probe_res.stderr})
            return False, report

        probe_data = json.loads(probe_res.stdout.strip().splitlines()[-1])
        report["interpreter_meta"] = probe_data.get("interpreter", {})
        report["packages"] = probe_data.get("packages", [])
        report["sys_path"] = probe_data.get("sys_path", [])
        report["module_files"] = probe_data.get("modules", {})

        print(f"✅ 模块来源隔离与业务缺席断言通过 (扫描 {len(report['module_files'])} 个已加载模块)")
        print("✅ memory_engine 缺包平滑降级断言通过 (MEMORY_AVAILABLE=False)")
        report["steps"].append({"name": "isolated_import_assertions", "passed": True})

        # Step 5: 写入测试配置并启动 main.py 子进程 (监听回环端口)
        auth_token = "clean-core-token-test-2026"
        core_config = {
            "app": {"mode": "core"},
            "llm": {
                "default": "mock-model",
                "models": {
                    "mock-model": {
                        "driver": "openai",
                        "model": "mock-model-v1",
                        "api_key": "mock-api-key",
                        "base_url": f"http://127.0.0.1:{mock_llm_port}/v1",
                        "tags": ["text"],
                    }
                },
            },
            "channels": {
                "api": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 0,
                    "auth_token": auth_token,
                }
            },
        }

        cfg_path = os.path.join(tmp_dir, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(core_config, f, indent=2)

        subproc_env = os.environ.copy()
        for k in list(subproc_env.keys()):
            if k.startswith("LITE_AGENT_"):
                subproc_env.pop(k, None)
        subproc_env.pop("PYTHONPATH", None)
        subproc_env.pop("MAIL_STATEMENT_PARSER_DIR", None)
        subproc_env["PYTHONUNBUFFERED"] = "1"
        subproc_env["LITE_AGENT_CONFIG_FILE"] = cfg_path
        subproc_env["LITE_AGENT_CONFIG_ROOT"] = tmp_dir

        proc = subprocess.Popen(
            [venv_python, "-u", "main.py", "--mode", "core", "--config", cfg_path],
            cwd=tmp_dir,
            env=subproc_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # 使用非阻塞线程安全队列读取输出，彻底消除阻塞卡死风险
        output_queue: queue.Queue = queue.Queue()
        output_buffer: List[str] = []

        def _reader_thread(pipe, q):
            try:
                for line in iter(pipe.readline, ''):
                    q.put(line)
            except Exception:
                pass
            finally:
                pipe.close()

        reader_t = threading.Thread(target=_reader_thread, args=(proc.stdout, output_queue), daemon=True)
        reader_t.start()

        api_port = None
        start_wait = time.time()

        while time.time() - start_wait < 20:
            if proc.poll() is not None:
                break
            try:
                line = output_queue.get(timeout=0.2)
                output_buffer.append(line)
                m = re.search(r"API Server 启动成功 \(http://127\.0\.0\.1:(\d+)\)", line)
                if m:
                    api_port = int(m.group(1))
                    break
            except queue.Empty:
                continue

        if api_port is None:
            while not output_queue.empty():
                try:
                    output_buffer.append(output_queue.get_nowait())
                except queue.Empty:
                    break
            proc_out = "".join(output_buffer)
            print(f"❌ API Server 启动超时或未捕获端口:\n{proc_out}")
            if proc.poll() is None:
                proc.kill()
            report["steps"].append({"name": "start_main_subproc", "passed": False, "error": proc_out})
            return False, report

        print(f"✅ main.py 独立子进程启动成功，绑定回环端口: {api_port}")
        report["steps"].append({"name": "start_main_subproc", "passed": True, "port": api_port})

        base_url = f"http://127.0.0.1:{api_port}"

        # Step 6: HTTP 端点实测验收
        # 6.1 健康端点与状态端点检查
        req_health = urllib.request.Request(f"{base_url}/health")
        with urllib.request.urlopen(req_health, timeout=5) as resp:
            health_json = json.loads(resp.read().decode("utf-8"))
            assert health_json.get("status") == "healthy"
            assert health_json.get("mode") == "core"

        req_api_health = urllib.request.Request(f"{base_url}/api/v1/health")
        with urllib.request.urlopen(req_api_health, timeout=5) as resp:
            assert resp.status == 200

        req_dashboard = urllib.request.Request(f"{base_url}/api/v1/dashboard")
        with urllib.request.urlopen(req_dashboard, timeout=5) as resp:
            dash_json = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 200
            assert isinstance(dash_json, list)
            assert [item["name"] for item in dash_json] == ["/new"], dash_json

        print("✅ 6.1 GET /health 与 /api/v1/dashboard 健康状态端点验证通过")
        report["steps"].append({"name": "http_health_check", "passed": True})

        # 6.2 鉴权与权限隔离检查 (401 / 403 / 200)
        # 401: 未鉴权
        try:
            urllib.request.urlopen(urllib.request.Request(f"{base_url}/api/v1/sessions"), timeout=5)
            raise AssertionError("未鉴权请求应被 401 拦截")
        except urllib.error.HTTPError as e:
            assert e.code == 401

        # 403: 错误 Token
        try:
            req_bad = urllib.request.Request(f"{base_url}/api/v1/sessions")
            req_bad.add_header("Authorization", "Bearer wrong-token-test")
            urllib.request.urlopen(req_bad, timeout=5)
            raise AssertionError("错误 Token 应被 403 拦截")
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # 200: 正确 Token
        req_auth = urllib.request.Request(f"{base_url}/api/v1/sessions")
        req_auth.add_header("Authorization", f"Bearer {auth_token}")
        with urllib.request.urlopen(req_auth, timeout=5) as resp:
            assert resp.status == 200

        print("✅ 6.2 鉴权与权限隔离 (401/403/200) 验证通过")
        report["steps"].append({"name": "http_auth_isolation", "passed": True})

        # 6.3 未启用业务路由严格 404 (非 500, 非 ImportError)
        unregistered = [
            ("GET", "/api/v1/review-meetings"),
            ("POST", "/api/v1/review-meetings/start"),
            ("GET", "/api/v1/todos"),
            ("POST", "/api/v1/billing"),
            ("GET", "/api/pull_task"),
        ]
        for meth, path in unregistered:
            try:
                data = b"{}" if meth == "POST" else None
                req_unreg = urllib.request.Request(f"{base_url}{path}", data=data, method=meth)
                req_unreg.add_header("Authorization", f"Bearer {auth_token}")
                if meth == "POST":
                    req_unreg.add_header("Content-Type", "application/json")
                urllib.request.urlopen(req_unreg, timeout=5)
                raise AssertionError(f"未启用业务端点 {meth} {path} 应返回 404")
            except urllib.error.HTTPError as e:
                assert e.code == 404, f"端点 {path} 返回了非 404 状态码: {e.code}"

        print("✅ 6.3 未启用业务路由 (404 隔离) 验证通过")
        report["steps"].append({"name": "http_unregistered_404", "passed": True})

        # 6.4 普通对话与假模型端到端调用
        chat_body = json.dumps({
            "session_id": "clean_core_user_1",
            "text": "测试基础对话",
        }).encode("utf-8")
        req_chat = urllib.request.Request(f"{base_url}/api/v1/chat", data=chat_body, method="POST")
        req_chat.add_header("Authorization", f"Bearer {auth_token}")
        req_chat.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req_chat, timeout=10) as resp:
            assert resp.status == 200
            chat_data = json.loads(resp.read().decode("utf-8"))
            assert chat_data.get("status") == "completed"
            assert "Hello from" in chat_data.get("response", "")

        print("✅ 6.4 普通对话 POST /api/v1/chat 验证通过")
        report["steps"].append({"name": "http_chat_dialogue", "passed": True})

        # 6.5 流式对话调用 (/v1/chat/completions stream=true) 深度事件流验证
        stream_body = json.dumps({
            "model": "mock-model",
            "messages": [{"role": "user", "content": "测试流式"}],
            "stream": True,
        }).encode("utf-8")
        req_stream = urllib.request.Request(
            f"{base_url}/v1/chat/completions", data=stream_body, method="POST"
        )
        req_stream.add_header("Authorization", f"Bearer {auth_token}")
        req_stream.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req_stream, timeout=10) as resp:
            assert resp.status == 200
            sse_lines = []
            while True:
                try:
                    line = resp.readline()
                except (TimeoutError, socket.timeout):
                    # 宿主在完整响应后仍保持连接；读取尾部检测重复终态，
                    # 只有已收到终态时，空闲超时才代表本次验收读完。
                    if "data: [DONE]" in sse_lines:
                        break
                    raise
                if not line:
                    break
                line_str = line.decode("utf-8").strip()
                if line_str:
                    sse_lines.append(line_str)

        reconstructed_text = ""
        done_count = 0
        terminal_finish_reason = None

        for line in sse_lines:
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                done_count += 1
                continue
            chunk_json = json.loads(payload)
            assert not chunk_json.get("error"), f"SSE 返回错误: {chunk_json['error']}"
            choices = chunk_json.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                if "content" in delta:
                    reconstructed_text += delta["content"]
                if choices[0].get("finish_reason"):
                    terminal_finish_reason = choices[0]["finish_reason"]

        assert len(reconstructed_text) > 0, "SSE 重建文本为空"
        assert reconstructed_text in (
            "Hello from clean core stream!",
            "Hello from clean core mock server!",
        ), f"SSE 文本重建不符: {reconstructed_text!r}"
        assert done_count == 1, f"SSE 终态标记 [DONE] 必须且仅出现一次，实际: {done_count}"

        print("✅ 6.5 流式对话 POST /v1/chat/completions (SSE 事件解析与文本验证) 验证通过")
        report["steps"].append({"name": "http_stream_dialogue", "passed": True})

        # Step 7: 优雅退出 (SIGINT) 并严格断言真实 returncode == 0
        proc.send_signal(signal.SIGINT)
        try:
            exit_code = proc.wait(timeout=8)
            assert exit_code == 0, f"进程退出码非 0: {exit_code}"
            print("✅ 7. 捕获 SIGINT 并优雅安全退出 (真实退出码 0)")
            report["steps"].append({"name": "subproc_graceful_shutdown", "passed": True, "exit_code": exit_code})
        except subprocess.TimeoutExpired:
            proc.kill()
            print("❌ 进程未在规定时间内退出")
            report["steps"].append({"name": "subproc_graceful_shutdown", "passed": False})
            return False, report

        report["passed"] = True
        return True, report

    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        if mock_llm_server:
            try:
                mock_llm_server.shutdown()
                mock_llm_server.server_close()
            except Exception:
                pass
        if not keep_temp:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    args = parse_args()
    agent_root = os.path.abspath(args.agent_root) if args.agent_root else os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..")
    )
    print("==================================================")
    print("🚀 启动 S3-A2 干净环境独立运行证明")
    print(f"  Agent 仓库: {agent_root}")
    print(f"  当前 Python: {sys.executable} ({sys.version.split()[0]})")
    print("==================================================")

    success, report = run_clean_core_verification(
        agent_root=agent_root,
        keep_temp=args.keep_temp,
        find_links=args.find_links,
    )
    report_path = args.report_json
    if not report_path:
        fd, report_path = tempfile.mkstemp(prefix="kernel-core-proof-", suffix=".json")
        os.close(fd)
    with open(report_path, "w", encoding="utf-8") as evidence_file:
        json.dump(report, evidence_file, ensure_ascii=False, indent=2)
    print(f"📄 完整验收证据: {os.path.abspath(report_path)}")

    print("\n==================================================")
    print("📊 S3-A2 验证汇总报告")
    print("==================================================")
    for step in report.get("steps", []):
        icon = "✅ PASS" if step.get("passed") else "❌ FAIL"
        print(f"{icon} | {step['name']:<30}")

    if success:
        print("🎉 恭喜！阶段三 S3-A2 干净环境独立运行证明全部通过！")
        sys.exit(0)
    else:
        print("⚠️ 干净环境独立运行验证存在未通过项。")
        sys.exit(1)


if __name__ == "__main__":
    main()
