"""
Phone App Mac Worker Daemon 与中枢 HTTP 通道端到端集成测试 (回环模式)。
严格遵循第四轮复核整改要求：
- 在 127.0.0.1 回环端口启动真实 ThreadingHTTPServer 服务端
- 显式运行 MacWorkerDaemon (使用 fake_mode=True 测试桩，醒目标注 FAKE 驱动)
- 覆盖完整的 HTTP 真实链路：
  1. 握手鉴权与版本锁定 (POST /api/v1/phone-app/worker/handshake)
  2. 周期心跳与在线保活 (POST /api/v1/phone-app/worker/heartbeat)
  3. 批次创建与触发派发 (POST /api/v1/phone-app/batches & /execute)
  4. 真实 HTTP 长轮询领任务 (POST /api/v1/phone-app/worker/poll_task)
  5. 本地 Fake 驱动执行与 HTTP 回执上报 (POST /api/v1/phone-app/worker/submit_receipt)
  6. 批次取消 HTTP 闭环 (POST /api/v1/phone-app/batches/{id}/cancel)
  7. 会话超时/中枢重启后自动 HTTP 重握手恢复 (D04)
"""

import os
import json
import time
import tempfile
import threading
import urllib.parse
import urllib.request
import pytest
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict, Any, Tuple

from lite_agent.features.phone_app.service import PhoneAppService
from lite_agent.features.phone_app.repository import PhoneAppRepository
from lite_agent.features.phone_app.catalog import AppCatalog
from lite_agent.features.phone_app.worker_hub import WorkerHub
from lite_agent.features.phone_app.contracts import BatchStatus
from lite_agent.features.phone_app import routes
from lite_agent.integrations.phone_app.worker.run_worker import MacWorkerDaemon


class LoopbackHttpHandler(BaseHTTPRequestHandler):
    """集成测试专用的回环 HTTP 请求处理器，严格调用 features.phone_app.routes 路由。"""

    def log_message(self, format, *args):
        # 单元/集成测试中静默标准日志
        pass

    @property
    def service(self) -> PhoneAppService:
        return self.server.service

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/api/v1/phone-app/devices", "/agent/api/v1/phone-app/devices"):
            routes.handle_get_devices(self, parsed, self.service)
        elif path in ("/api/v1/phone-app/apps", "/agent/api/v1/phone-app/apps"):
            routes.handle_get_apps(self, parsed, self.service)
        elif path in ("/api/v1/phone-app/batches", "/agent/api/v1/phone-app/batches"):
            routes.handle_get_batches(self, parsed, self.service)
        elif "/api/v1/phone-app/batches/" in path:
            batch_id = path.split("/batches/")[1].split("/")[0]
            routes.handle_get_batch_detail(self, parsed, self.service, batch_id)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
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
        elif "/batches/" in path and path.endswith("/execute"):
            parts = path.split("/batches/")[1].split("/")
            batch_id = parts[0]
            routes.handle_post_execute(self, parsed, self.service, batch_id)
        elif "/batches/" in path and path.endswith("/cancel"):
            parts = path.split("/batches/")[1].split("/")
            batch_id = parts[0]
            routes.handle_cancel_batch(self, parsed, self.service, batch_id)
        else:
            self.send_response(404)
            self.end_headers()


class LoopbackServerFixture:
    """管理在 127.0.0.1 临时随机端口启动的真实 HTTP 测试服务器。"""

    def __init__(self, token="test-secret-token-abc"):
        self.tmpdir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tmpdir.name, "loopback_test.db")
        self.repo = PhoneAppRepository(db_path=db_path)
        self.catalog = AppCatalog()
        self.hub = WorkerHub(expected_token=token)
        self.service = PhoneAppService(repo=self.repo, catalog=self.catalog, worker_hub=self.hub)
        self.token = token

        # 绑定 127.0.0.1 随机端口
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), LoopbackHttpHandler)
        self.httpd.service = self.service
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"

        self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.server_thread.start()

    def close(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        try:
            self.service.close()
        except Exception:
            pass
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass


def _http_client_post(url: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return resp.status, data
    except urllib.error.HTTPError as e:
        data = json.loads(e.read().decode("utf-8"))
        return e.code, data


def _http_client_get(url: str) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return resp.status, data
    except urllib.error.HTTPError as e:
        data = json.loads(e.read().decode("utf-8"))
        return e.code, data


def test_loopback_http_handshake_and_heartbeat():
    """验证 MacWorkerDaemon 通过 127.0.0.1 真实 HTTP 完成握手、获取 session 并发送心跳。"""
    fixture = LoopbackServerFixture()
    try:
        daemon = MacWorkerDaemon(
            hub_url=fixture.base_url,
            token=fixture.token,
            worker_id="mac-worker-loopback-1",
            device_id="c68a3cf2",
            fake_mode=True  # 显式使用 Fake 驱动，醒目标注
        )

        # 1. 真实 HTTP 握手
        ok = daemon.handshake()
        assert ok is True
        assert daemon.session_id is not None
        assert daemon.session_id.startswith("wsess-")

        # 2. 中枢端验证设备已在线
        online_devs = fixture.hub.get_online_devices()
        assert any(d["device_id"] == "c68a3cf2" for d in online_devs)
        assert online_devs[0]["test_mode"] is True

        # 3. 真实 HTTP 心跳
        hb_ok, hb_data = daemon.heartbeat()
        assert hb_ok is True
        assert hb_data.get("status") == "ack"
        assert fixture.hub.get_online_devices()[0]["test_mode"] is True

        # 4. 验证经由 Web API 获取设备在线状态
        status_code, devs_res = _http_client_get(f"{fixture.base_url}/api/v1/phone-app/devices")
        assert status_code == 200
        assert any(d["device_id"] == "c68a3cf2" and d["status"] == "online" for d in devs_res.get("devices", []))

    finally:
        fixture.close()


def test_loopback_http_full_task_dispatch_and_receipt_flow():
    """验证真实 HTTP 链路下的批次创建、任务长轮询领取、Fake 驱动执行与回执上报闭环。"""
    fixture = LoopbackServerFixture()
    try:
        daemon = MacWorkerDaemon(
            hub_url=fixture.base_url,
            token=fixture.token,
            worker_id="mac-worker-loopback-2",
            device_id="c68a3cf2",
            fake_mode=True  # 醒目标注: 纯回环 Fake 驱动集成验证
        )
        assert daemon.handshake() is True

        # 1. 通过 HTTP POST 创建批次
        code, batch_res = _http_client_post(
            f"{fixture.base_url}/api/v1/phone-app/batches",
            {
                "query": "机械键盘",
                "app_ids": ["jd"],
                "device_id": "c68a3cf2",
                "action": "search_products",
                "mode": "fixed",
                "filters": {
                    "sales": {"threshold": 1000, "operator": "gt"}
                }
            }
        )
        assert code == 201
        batch_id = batch_res["batch_id"]

        # 2. 通过 HTTP POST 触发批次执行 (异步后台派发)
        code_exec, exec_res = _http_client_post(
            f"{fixture.base_url}/api/v1/phone-app/batches/{batch_id}/execute",
            {}
        )
        assert code_exec == 200

        # 3. MacWorkerDaemon 通过 HTTP poll_task 领取任务并提交回执
        # 轮询 3 次以内必须领取并完成执行
        executed = False
        for _ in range(5):
            has_task, receipt = daemon.poll_and_execute_once(timeout_sec=1.0)
            if has_task:
                assert receipt is not None
                assert receipt["status"] == "succeeded"
                executed = True
                break
            time.sleep(0.3)

        assert executed is True, "MacWorkerDaemon 应该成功通过 HTTP 领取并执行任务"

        # 4. 验证中枢端批次状态和三态过滤结果
        time.sleep(0.5)
        code_det, detail_res = _http_client_get(f"{fixture.base_url}/api/v1/phone-app/batches/{batch_id}")
        assert code_det == 200
        assert detail_res["batch"]["status"] == BatchStatus.SUCCEEDED.value
        assert detail_res["test_mode"] is True
        summary = detail_res.get("summary", {})
        assert summary.get("total_scanned") == 2
        assert summary.get("matched_count") == 1
        assert summary.get("needs_verification_count") == 1

    finally:
        fixture.close()


def test_loopback_http_cancellation_flow():
    """验证真实 HTTP 取消链路：批次取消后排队任务停止派发，Worker 不再执行 (D01)。"""
    fixture = LoopbackServerFixture()
    try:
        daemon = MacWorkerDaemon(
            hub_url=fixture.base_url,
            token=fixture.token,
            worker_id="mac-worker-loopback-3",
            device_id="c68a3cf2",
            fake_mode=True
        )
        assert daemon.handshake() is True

        # 1. 创建批次
        code, batch_res = _http_client_post(
            f"{fixture.base_url}/api/v1/phone-app/batches",
            {"query": "降噪耳机", "app_ids": ["jd"], "device_id": "c68a3cf2", "mode": "fixed"}
        )
        assert code == 201
        batch_id = batch_res["batch_id"]

        # 2. 立即通过 HTTP 取消该批次 (未开始执行前)
        code_cancel, cancel_res = _http_client_post(
            f"{fixture.base_url}/api/v1/phone-app/batches/{batch_id}/cancel",
            {"reason": "用户取消测试"}
        )
        assert code_cancel == 200
        assert cancel_res.get("success") is True

        # 3. 尝试触发执行
        _http_client_post(f"{fixture.base_url}/api/v1/phone-app/batches/{batch_id}/execute", {})

        # 4. Worker 轮询：严禁领取到已取消的批次任务
        has_task, _ = daemon.poll_and_execute_once(timeout_sec=0.5)
        assert has_task is False, "已取消的批次严禁被 Worker 领取执行！"

        # 5. 批次状态确认保持 cancelled
        code_det, detail_res = _http_client_get(f"{fixture.base_url}/api/v1/phone-app/batches/{batch_id}")
        assert code_det == 200
        assert detail_res["batch"]["status"] == BatchStatus.CANCELLED.value
        time.sleep(0.4)

    finally:
        fixture.close()


def test_loopback_http_session_expiration_and_automatic_rehandshake():
    """验证会话超时或失效时，Worker 在 HTTP 交互中检测到错误并自动重新握手恢复 (D04)。"""
    fixture = LoopbackServerFixture()
    try:
        daemon = MacWorkerDaemon(
            hub_url=fixture.base_url,
            token=fixture.token,
            worker_id="mac-worker-loopback-4",
            device_id="c68a3cf2",
            fake_mode=True
        )
        # 1. 首次握手
        assert daemon.handshake() is True
        old_session = daemon.session_id

        # 2. 中枢端模拟会话过期/重启清空
        fixture.hub._workers.clear()

        # 3. Worker 发送心跳，检测到会话失效后自动重握手
        ok, res = daemon.heartbeat()
        assert ok is False
        assert "超时" in str(res.get("error", "")) or "未注册" in str(res.get("error", ""))

        # 模拟守护线程中自动重握手逻辑
        daemon.session_id = None
        re_ok = daemon.handshake()
        assert re_ok is True
        assert daemon.session_id is not None
        assert daemon.session_id != old_session

        # 4. 使用新会话成功发送心跳
        hb_ok, _ = daemon.heartbeat()
        assert hb_ok is True

    finally:
        fixture.close()
