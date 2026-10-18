"""
Mac 本地执行端独立工作进程 (lite_agent.integrations.phone_app.worker.run_worker).
由 Mac 本地启动，主动向 VPS1 中枢发起 HTTP/HTTPS 连接、握手鉴权、独立线程心跳保活并循环领取执行任务。
严格遵循 B02、C01 与 C03 要求：
- 独立可启动、严格环境变量/参数鉴权（严禁默认弱口令）。
- 独立后台心跳线程（执行长任务不阻塞保活）。
- 接收中枢任务取消通知并向执行器注入 cancel_event 及时中止。
- 回执提交携带有效会话 session_id 并处理异常。
"""

from __future__ import annotations
import os
import sys
import time
import json
import signal
import logging
import argparse
import threading
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [MacWorker] %(message)s"
)
logger = logging.getLogger("mac_worker")


class MacWorkerDaemon:
    """Mac 端主动连接工作端守护程序 (C01 & C03)。"""

    VERSION = "1.0.0"

    def __init__(
        self,
        hub_url: str = "http://127.0.0.1:8000",
        token: Optional[str] = None,
        worker_id: str = "mac-worker-oneplus9",
        device_id: str = "c68a3cf2",
        poll_interval: float = 2.0,
        heartbeat_interval_sec: float = 8.0,
        fake_mode: bool = False
    ):
        token = token or os.getenv("PHONE_WORKER_TOKEN")
        if not token:
            raise ValueError(
                "必须提供 Worker 鉴权 Token (--token 或环境变量 PHONE_WORKER_TOKEN)。禁止使用硬编码默认口令。"
            )

        import urllib.parse
        parsed = urllib.parse.urlparse(hub_url)
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"不支持的 URL 协议: '{scheme}'，仅支持 http 或 https")

        # 传输安全校验 (D04): 远程主机严禁使用明文 HTTP，HTTP 仅允许明确的回环地址
        is_loopback = hostname in ("127.0.0.1", "localhost", "::1")
        if scheme == "http" and not is_loopback:
            raise ValueError(
                f"传输安全阻断 (D04): 远程主机 '{hostname}' 禁止使用明文 HTTP 通信，必须使用 HTTPS 连接中枢以防凭证与任务数据泄露"
            )

        self.hub_url = hub_url.rstrip("/")
        self.token = token
        self.worker_id = worker_id
        self.device_id = device_id
        self.poll_interval = poll_interval
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self.fake_mode = fake_mode
        self.session_id: Optional[str] = None
        self._running = True

        self._current_lease_id: Optional[str] = None
        self._current_cancel_event = threading.Event()
        self._lock = threading.Lock()

        if not self.fake_mode:
            from lite_agent.integrations.phone_app.worker.worker import MacWorkerClient
            self.client = MacWorkerClient(device_id=device_id)
        else:
            from lite_agent.integrations.phone_app.worker.worker import FakeWorkerClient
            self.client = FakeWorkerClient(device_id=device_id)

    def _http_post(self, path: str, payload: Dict[str, Any], timeout: float = 10.0) -> Tuple[bool, Dict[str, Any]]:
        urls_to_try = [f"{self.hub_url}{path}", f"{self.hub_url}/agent{path}"]
        last_err = None

        for url in urls_to_try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"}
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return True, data
            except urllib.error.HTTPError as e:
                try:
                    err_data = json.loads(e.read().decode("utf-8"))
                    return False, err_data
                except Exception:
                    last_err = f"HTTP {e.code}: {e.reason}"
            except Exception as e:
                last_err = str(e)

        return False, {"error": last_err or "请求中枢失败"}

    def handshake(self) -> bool:
        """向中枢握手注册设备能力。"""
        is_online = self.client.is_online() if hasattr(self.client, "is_online") else True
        devices = [
            {
                "device_id": self.device_id,
                "model": "OnePlus 9 (LE2110)",
                "android_version": 14,
                "status": "online" if is_online else "offline",
                "validated": not self.fake_mode,
                "test_mode": self.fake_mode
            }
        ]

        logger.info(f"正在向中枢 {self.hub_url} 发起握手鉴权 (WorkerID={self.worker_id}, Version={self.VERSION})...")
        ok, res = self._http_post("/api/v1/phone-app/worker/handshake", {
            "worker_id": self.worker_id,
            "token": self.token,
            "version": self.VERSION,
            "capabilities": ["jd"],
            "devices": devices
        })

        if not ok:
            logger.error(f"中枢握手失败: {res.get('error')}")
            return False

        self.session_id = res.get("session_id")
        self.heartbeat_interval_sec = float(res.get("heartbeat_interval_sec", 10.0))
        logger.info(f"中枢握手成功！会话 ID: {self.session_id}, 心跳间隔: {self.heartbeat_interval_sec}s")
        return True

    def heartbeat(self) -> Tuple[bool, Dict[str, Any]]:
        """发送单次心跳 (C03: 传递设备真实状态)。"""
        if not self.session_id:
            return False, {"error": "未建立有效会话"}

        is_online = self.client.is_online() if hasattr(self.client, "is_online") else True
        devices = [
            {
                "device_id": self.device_id,
                "model": "OnePlus 9 (LE2110)",
                "android_version": 14,
                "status": "online" if is_online else "offline",
                "validated": not self.fake_mode,
                "test_mode": self.fake_mode
            }
        ]
        return self._http_post("/api/v1/phone-app/worker/heartbeat", {
            "worker_id": self.worker_id,
            "session_id": self.session_id,
            "devices": devices
        }, timeout=5.0)

    def _heartbeat_daemon(self):
        """独立心跳后台线程：任务执行阻塞时不影响心跳，并实时监听中枢取消通知 (C03)。"""
        logger.info("Mac Worker 心跳保活后台线程已启动")
        while self._running:
            time.sleep(self.heartbeat_interval_sec)
            if not self._running:
                break
            if not self.session_id:
                continue

            try:
                ok, res = self.heartbeat()
                if ok:
                    cancelled_leases = res.get("cancelled_leases", [])
                    with self._lock:
                        if self._current_lease_id and self._current_lease_id in cancelled_leases:
                            logger.warning(
                                f"收到中枢取消租约通知: '{self._current_lease_id}'，立即触发执行器中断"
                            )
                            self._current_cancel_event.set()
                else:
                    err_msg = str(res.get("error", ""))
                    logger.warning(f"心跳保活失败: {err_msg}")
                    # 若会话失效/超时/未握手/中枢重启，重置 session_id 并主动重新握手 (D04)
                    if any(kw in err_msg for kw in ("会话", "超时", "未握手", "未注册", "失效", "401")):
                        logger.warning("检测到会话已失效或中枢重启，正在重新发起握手 (D04)...")
                        self.session_id = None
                        if self.handshake():
                            logger.info("重握手成功，会话已恢复 (D04)")
            except Exception as e:
                logger.warning(f"心跳保活网络异常: {e}")

    def poll_and_execute_once(self, timeout_sec: float = 3.0) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """执行单次任务轮询、本地执行与回执提交。返回 (has_task, receipt_data)。"""
        ok, res = self._http_post("/api/v1/phone-app/worker/poll_task", {
            "worker_id": self.worker_id,
            "session_id": self.session_id,
            "timeout_sec": timeout_sec
        }, timeout=timeout_sec + 3.0)

        if not ok:
            err_msg = str(res.get("error", ""))
            if any(kw in err_msg for kw in ("会话", "超时", "未握手", "未注册", "失效", "401")):
                logger.warning("任务轮询检测到会话失效，重新发起握手 (D04)...")
                self.session_id = None
                self.handshake()
            return False, None

        if ok and res.get("has_task"):
            lease_id = res["lease_id"]
            task = res["task"]
            batch = res["batch"]
            logger.info(
                f"领取到新任务租约: {lease_id}, TaskID={task.get('task_id')}, App={task.get('app_id')}"
            )

            with self._lock:
                self._current_lease_id = lease_id
                self._current_cancel_event = threading.Event()

            # 执行任务，支持中途取消信号响应 (C03)
            try:
                receipt = self.client.execute_task(
                    task=task,
                    batch=batch,
                    cancel_event=self._current_cancel_event
                )
            finally:
                with self._lock:
                    self._current_lease_id = None
                    self._current_cancel_event = None

            # 上报回执，携带 session_id (C01)
            sub_ok, sub_res = self._http_post("/api/v1/phone-app/worker/submit_receipt", {
                "worker_id": self.worker_id,
                "session_id": self.session_id,
                "lease_id": lease_id,
                "receipt": receipt
            })
            if sub_ok:
                logger.info(f"回执上报成功: status={receipt.get('status')}")
            else:
                logger.error(f"回执上报失败: {sub_res}")
            return True, receipt

        return False, None

    def run(self):
        """主轮询循环。"""
        try:
            signal.signal(signal.SIGINT, self._handle_exit)
            signal.signal(signal.SIGTERM, self._handle_exit)
        except (ValueError, AttributeError):
            pass

        # 1. 握手连接
        while self._running:
            if self.handshake():
                break
            logger.warning("握手失败，5 秒后重试...")
            time.sleep(5.0)

        # 启动独立心跳与取消通知监听线程 (C03)
        hb_thread = threading.Thread(target=self._heartbeat_daemon, daemon=True)
        hb_thread.start()

        logger.info("Mac Worker 进入任务租约轮询监听模式...")

        while self._running:
            try:
                self.poll_and_execute_once(timeout_sec=3.0)
            except Exception as e:
                logger.error(f"轮询或执行异常: {e}")

            time.sleep(self.poll_interval)

        logger.info("Mac Worker 已安全停止。")

    def _handle_exit(self, signum, frame):
        logger.info(f"收到终止信号 ({signum})，正在退出守护程序...")
        self._running = False
        with self._lock:
            self._current_cancel_event.set()


def main():
    parser = argparse.ArgumentParser(description="LiteAgent Mac Worker 节点守护程序")
    parser.add_argument("--hub-url", default=os.getenv("PHONE_HUB_URL", "http://127.0.0.1:8000"), help="中枢地址")
    parser.add_argument("--token", default=os.getenv("PHONE_WORKER_TOKEN", ""), help="Worker 鉴权令牌 (禁止硬编码弱口令)")
    parser.add_argument("--worker-id", default=os.getenv("PHONE_WORKER_ID", "mac-worker-oneplus9"), help="Worker 唯一标识")
    parser.add_argument("--device-id", default=os.getenv("PHONE_DEVICE_ID", "c68a3cf2"), help="物理设备序列号")
    parser.add_argument("--fake", action="store_true", help="使用测试桩模式 (非真机)")
    args = parser.parse_args()

    if not args.token:
        print("错误: 必须通过 --token 或环境变量 PHONE_WORKER_TOKEN 指定认证令牌。禁止使用默认口令。")
        sys.exit(1)

    daemon = MacWorkerDaemon(
        hub_url=args.hub_url,
        token=args.token,
        worker_id=args.worker_id,
        device_id=args.device_id,
        fake_mode=args.fake
    )
    daemon.run()


if __name__ == "__main__":
    main()
