"""
本地浏览器联调与验证服务 (dev/tests/support/browser_test_server.py).
启动集成 HTTP 服务，提供：
1. web_dashboard 前端静态资源 (HTML, CSS, JS) 并具备目录遍历防御与相对路径严格判定
2. /agent/api/v1/phone-app/... 全量后端接口（增删查改、草稿编辑 PUT、审批、驳回、执行、取消）
3. /agent/api/v1/auth 快捷测试免密通过
4. MacWorkerDaemon 本地回环 Fake 守护进程 (醒目标注 FAKE 模式)
"""

import os
import sys
import json
import time
import mimetypes
import tempfile
import threading
import urllib.parse
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from lite_agent.core.constants import PROJECT_ROOT

# 使用 python -m dev.tests.support.browser_test_server 启动，不注入导入路径。
proj_root = PROJECT_ROOT

from lite_agent.features.phone_app.service import PhoneAppService
from lite_agent.features.phone_app.repository import PhoneAppRepository
from lite_agent.features.phone_app.catalog import AppCatalog
from lite_agent.features.phone_app.worker_hub import WorkerHub
from lite_agent.features.phone_app import routes
from lite_agent.integrations.phone_app.worker.run_worker import MacWorkerDaemon
from lite_agent.core.llm_gateway import LLMGateway

WEB_DIR = os.path.join(proj_root, "web_dashboard")
TEST_PORT = 18080
SHARED_TOKEN = "local-browser-test-token-2026"


class FakeScriptRouter:
    """仅供本地页面测试的离线模型替身；不调用模型或手机。"""

    models_cfg = {"fake-browser-model": {}}

    def get_invoker(self, model, **kwargs):
        return self

    def get_driver(self, model):
        return "test_fake"

    def invoke_sync(self, messages, tools=None, **kwargs):
        return {"content": json.dumps({"jd": [
            {"action": "open_app", "package": "com.jingdong.app.mall"},
            {"action": "extract", "fields": ["title", "price"]},
        ]}), "usage_total": 0}


class BrowserTestHttpHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 调试日志打印
        sys.stderr.write(f"[HTTP {self.command}] {self.path} - {format % args}\n")

    @property
    def service(self) -> PhoneAppService:
        return self.server.service

    def _serve_static(self, rel_path: str):
        if rel_path in ("", "/"):
            rel_path = "index.html"
        rel_path = rel_path.lstrip("/")

        base_dir = Path(WEB_DIR).resolve()
        try:
            target_path = (base_dir / rel_path).resolve()
            # F01 防御: 使用 resolve + relative_to 严格杜绝目录穿越与越界符号链接
            target_path.relative_to(base_dir)
        except (ValueError, RuntimeError):
            self.send_error(403, "Forbidden: Directory Traversal Denied")
            return

        if not target_path.exists() or target_path.is_dir():
            self.send_error(404, f"File Not Found: {rel_path}")
            return

        full_path = str(target_path)
        mime_type, _ = mimetypes.guess_type(full_path)
        if not mime_type:
            mime_type = "application/octet-stream"
        if full_path.endswith(".js"):
            mime_type = "application/javascript; charset=utf-8"
        elif full_path.endswith(".css"):
            mime_type = "text/css; charset=utf-8"
        elif full_path.endswith(".html"):
            mime_type = "text/html; charset=utf-8"

        try:
            with open(full_path, "rb") as f:
                content = f.read()
            if target_path.suffix == ".html":
                content = content.replace(
                    b"<head>",
                    b'<head><script>window.PHONE_APP_TEST_MODE = true;</script>',
                    1,
                ).replace(
                    b"<body>",
                    '<body><div role="status" style="background:#fff3cd;color:#663c00;padding:12px;text-align:center;font-weight:bold">FAKE 测试环境：不操作手机；商品和模型输出均为离线测试数据</div>'.encode(),
                    1,
                )
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, str(e))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 1. Phone App API
        if path in ("/api/v1/phone-app/devices", "/agent/api/v1/phone-app/devices"):
            routes.handle_get_devices(self, parsed, self.service)
        elif path in ("/api/v1/phone-app/apps", "/agent/api/v1/phone-app/apps"):
            routes.handle_get_apps(self, parsed, self.service)
        elif path in ("/api/v1/phone-app/batches", "/agent/api/v1/phone-app/batches"):
            routes.handle_get_batches(self, parsed, self.service)
        elif "/phone-app/batches/" in path:
            batch_id = path.split("/batches/")[1].split("/")[0]
            routes.handle_get_batch_detail(self, parsed, self.service, batch_id)
        # 2. 静态页面与资源
        else:
            self._serve_static(path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 认证模拟
        if path in ("/agent/api/v1/auth", "/api/v1/auth"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            resp = {
                "success": True,
                "review_token": "mock-token-review",
                "scope": "review-meetings:host",
                "review_token_expires_at": int(time.time()) + 86400
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        # Phone App API
        if path in ("/api/v1/phone-app/worker/handshake", "/agent/api/v1/phone-app/worker/handshake"):
            routes.handle_worker_handshake(self, parsed, self.service)
        elif path in ("/api/v1/phone-app/worker/heartbeat", "/agent/api/v1/phone-app/worker/heartbeat"):
            routes.handle_worker_heartbeat(self, parsed, self.service)
        elif path in ("/api/v1/phone-app/worker/poll_task", "/agent/api/v1/phone-app/worker/poll_task"):
            routes.handle_worker_poll_task(self, parsed, self.service)
        elif path in ("/api/v1/phone-app/worker/submit_receipt", "/agent/api/v1/phone-app/worker/submit_receipt"):
            routes.handle_worker_submit_receipt(self, parsed, self.service)
        elif path in ("/api/v1/phone-app/batches", "/agent/api/v1/phone-app/batches"):
            routes.handle_post_batches(self, parsed, self.service)
        elif "/batches/" in path and path.endswith("/generate"):
            parts = path.split("/batches/")[1].split("/")
            batch_id = parts[0]
            routes.handle_post_generate(self, parsed, self.service, batch_id)
        elif "/batches/" in path and path.endswith("/approve"):
            parts = path.split("/batches/")[1].split("/")
            batch_id = parts[0]
            routes.handle_post_approve(self, parsed, self.service, batch_id)
        elif "/batches/" in path and path.endswith("/reject"):
            parts = path.split("/batches/")[1].split("/")
            batch_id = parts[0]
            routes.handle_reject_batch(self, parsed, self.service, batch_id)
        elif "/batches/" in path and path.endswith("/execute"):
            parts = path.split("/batches/")[1].split("/")
            batch_id = parts[0]
            routes.handle_post_execute(self, parsed, self.service, batch_id)
        elif "/batches/" in path and path.endswith("/cancel"):
            parts = path.split("/batches/")[1].split("/")
            batch_id = parts[0]
            routes.handle_cancel_batch(self, parsed, self.service, batch_id)
        elif path in ("/api/v1/phone-app/devices/resume", "/agent/api/v1/phone-app/devices/resume"):
            routes.handle_resume_device(self, parsed, self.service)
        else:
            self.send_error(404, f"API Not Found: {path}")

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if "/batches/" in path and path.endswith("/draft"):
            parts = path.split("/batches/")[1].split("/")
            batch_id = parts[0]
            routes.handle_put_draft(self, parsed, self.service, batch_id)
        else:
            self.send_error(404, f"API Not Found: {path}")


def main():
    tmpdir = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmpdir.name, "browser_dashboard.db")
    print(f"初始化 Phone App 数据库: {db_path}")

    repo = PhoneAppRepository(db_path=db_path)
    catalog = AppCatalog()
    hub = WorkerHub(expected_token=SHARED_TOKEN)
    service = PhoneAppService(
        repo=repo, catalog=catalog, worker_hub=hub,
        router=FakeScriptRouter(), gateway=LLMGateway(),
        default_model="fake-browser-model",
    )

    server = ThreadingHTTPServer(("127.0.0.1", TEST_PORT), BrowserTestHttpHandler)
    server.service = service

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"✅ Dashboard 服务已启动: http://127.0.0.1:{TEST_PORT}")

    # 启动本地 Fake Worker 守护进程
    worker_daemon = MacWorkerDaemon(
        hub_url=f"http://127.0.0.1:{TEST_PORT}",
        token=SHARED_TOKEN,
        worker_id="mac-worker-browser-demo",
        device_id="c68a3cf2",
        fake_mode=True  # 醒目标注: 纯回环 Fake 驱动测试
    )

    worker_thread = threading.Thread(target=worker_daemon.run, daemon=True)
    worker_thread.start()
    print(f"✅ Fake Worker Daemon 已启动并在后台自动握手与长轮询")

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("停止服务...")
        server.shutdown()
        tmpdir.cleanup()


if __name__ == "__main__":
    main()
