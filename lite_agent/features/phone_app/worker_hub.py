"""
VPS1 中枢端 Worker 通道管理与任务租约调度中心 (features.phone_app.worker_hub).
负责管理外部 Mac Worker 节点的主动连接、鉴权握手、心跳保活、任务租约派发与回执同步。
中枢严格不直接运行本机 ADB，所有物理设备状态由真实 Worker 上报维护 (B02 & C01 & C03)。
"""

from __future__ import annotations
import time
import uuid
import secrets
import os
import threading
import logging
from typing import Dict, Any, List, Optional, Tuple, Set

logger = logging.getLogger("features.phone_app.worker_hub")


class TaskLease:
    """任务执行租约。"""

    def __init__(
        self,
        lease_id: str,
        device_id: str,
        task_id: str,
        attempt_id: str,
        task_data: Dict[str, Any],
        batch_data: Dict[str, Any],
        ttl_seconds: float = 180.0
    ):
        self.lease_id = lease_id
        self.device_id = device_id
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.task_data = task_data
        self.batch_data = batch_data
        self.created_at = time.monotonic()
        self.expires_at = self.created_at + ttl_seconds
        self.done_event = threading.Event()
        self.receipt: Optional[Dict[str, Any]] = None
        self.cancelled = False
        self.worker_id: Optional[str] = None


class WorkerHub:
    """中枢 Worker 通道聚合器与租约调度器 (C01 & C03: 严格会话认证、真实设备状态保留与取消监听)。"""

    SUPPORTED_VERSION = "1.0.0"
    ALLOWED_CAPABILITIES: Set[str] = {"jd"}

    def __init__(
        self,
        expected_token: Optional[str] = None,
        heartbeat_timeout: float = 30.0
    ):
        raw_env_token = os.environ.get("PHONE_WORKER_TOKEN")
        self.is_configured = bool(expected_token or raw_env_token)
        if not expected_token:
            expected_token = raw_env_token
        if not expected_token:
            expected_token = secrets.token_hex(32)
            logger.warning(
                "未配置 PHONE_WORKER_TOKEN 共享凭证，已自动生成生命周期高熵安全随机凭证。通道状态标记为未显式配置 (D04: unconfigured)。如需生产 Mac 接入请在环境变量中显式指定。"
            )
        self.expected_token = expected_token
        self.heartbeat_timeout = heartbeat_timeout
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._workers: Dict[str, Dict[str, Any]] = {}
        self._pending_leases: List[TaskLease] = []
        self._active_leases: Dict[str, TaskLease] = {}
        self._closed = False

    def is_ready(self) -> bool:
        """检查 WorkerHub 通道是否显式配置了共享凭证 (D04: 未配置凭证时不得显示已就绪)。"""
        return self.is_configured and not self._closed

    def get_status(self) -> Dict[str, Any]:
        """获取通道配置就绪状态披露 (D04)。"""
        return {
            "is_configured": self.is_configured,
            "is_ready": self.is_ready(),
            "status_text": "已就绪" if self.is_ready() else "未就绪 (缺失 PHONE_WORKER_TOKEN 显式配置)",
            "connected_workers": len(self._workers)
        }

    def close(self):
        with self._lock:
            self._closed = True
            for lease in list(self._pending_leases) + list(self._active_leases.values()):
                lease.cancelled = True
                lease.done_event.set()
            self._pending_leases.clear()
            self._active_leases.clear()
            self._cond.notify_all()

    def handshake(
        self,
        worker_id: str,
        token: str,
        version: str,
        capabilities: List[str],
        devices: List[Dict[str, Any]]
    ) -> Tuple[bool, Dict[str, Any]]:
        """处理 Mac Worker 主动握手鉴权与版本锁定 (C01: 凭证与能力强校验)。"""
        if self._closed:
            return False, {"error": "中枢 WorkerHub 已关闭"}

        if not worker_id or not isinstance(worker_id, str) or len(worker_id) > 64:
            return False, {"error": "无效的 worker_id"}

        # 1. 凭证常量时间对比防侧信道攻击
        if not token or not secrets.compare_digest(str(token), self.expected_token):
            logger.warning(f"Worker '{worker_id}' 握手失败: 认证 Token 无效")
            return False, {"error": "认证失败: Token 无效"}

        # 2. 版本兼容性校验 (版本锁定)
        if version != self.SUPPORTED_VERSION:
            logger.warning(f"Worker '{worker_id}' 握手失败: 协议版本不匹配 ({version} != {self.SUPPORTED_VERSION})")
            return False, {"error": f"协议版本不兼容: 预期 {self.SUPPORTED_VERSION}, 实际 {version}"}

        # 3. 校验上报的能力与设备列表
        for cap in capabilities:
            if cap not in self.ALLOWED_CAPABILITIES:
                return False, {"error": f"Worker 上报了未允许的能力 '{cap}'"}

        validated_devices = []
        for dev in devices:
            did = dev.get("device_id")
            if not did or not isinstance(did, str):
                return False, {"error": "设备列表中包含缺少 device_id 的无效项"}
            d_status = dev.get("status", "unknown")
            if d_status not in ("online", "offline", "unauthorized", "paused", "unknown"):
                return False, {"error": f"设备 '{did}' 状态 '{d_status}' 不合法"}
            validated_devices.append({
                "device_id": did,
                "model": dev.get("model", "Android Device"),
                "status": d_status,
                "test_mode": dev.get("test_mode") is True,
                "handsets_port": dev.get("handsets_port", 9008)
            })

        session_id = f"wsess-{uuid.uuid4().hex}"
        now = time.time()
        authorized_device_ids = {d["device_id"] for d in validated_devices}

        with self._lock:
            self._workers[worker_id] = {
                "worker_id": worker_id,
                "session_id": session_id,
                "version": version,
                "capabilities": capabilities,
                "devices": validated_devices,
                "authorized_device_ids": authorized_device_ids,
                "last_heartbeat": now,
                "connected_at": now,
            }
            logger.info(f"Worker '{worker_id}' 握手成功 session_id={session_id}, 包含设备={len(validated_devices)}")
            return True, {
                "session_id": session_id,
                "heartbeat_interval_sec": 10,
                "server_version": self.SUPPORTED_VERSION,
                "transport": "https_long_poll"
            }

    def _verify_session_locked(self, worker_id: str, session_id: str) -> Tuple[bool, Optional[str]]:
        worker = self._workers.get(worker_id)
        if not worker:
            return False, "Worker 未注册或未握手"
        if not session_id or not secrets.compare_digest(worker["session_id"], session_id):
            return False, "Worker 会话 Token 无效或已失效"
        if time.time() - worker["last_heartbeat"] > self.heartbeat_timeout:
            return False, "Worker 会话已心跳超时，请重新握手"
        return True, None

    def heartbeat(
        self,
        worker_id: str,
        session_id: str,
        devices: Optional[List[Dict[str, Any]]] = None
    ) -> Tuple[bool, Dict[str, Any]]:
        """接收心跳保活并同步真实设备状态 (C01 & C03: 校验会话并下发取消租约列表)。"""
        with self._lock:
            ok, err = self._verify_session_locked(worker_id, session_id)
            if not ok:
                return False, {"error": err}

            worker = self._workers[worker_id]
            worker["last_heartbeat"] = time.time()

            if devices is not None:
                authorized_ids = worker.get("authorized_device_ids", set())
                validated_devices = []
                for dev in devices:
                    did = dev.get("device_id")
                    if did and isinstance(did, str) and did in authorized_ids:
                        validated_devices.append({
                            "device_id": did,
                            "model": dev.get("model", "Android Device"),
                            "status": dev.get("status", "unknown"),
                            "test_mode": dev.get("test_mode") is True,
                            "handsets_port": dev.get("handsets_port", 9008)
                        })
                    elif did not in authorized_ids:
                        logger.warning(f"Worker '{worker_id}' 心跳上报了未在握手阶段授权的设备 '{did}'，已拦截并忽略 (D04)")
                worker["devices"] = validated_devices

            # 收集该 Worker 正在执行但被中枢取消的租约 ID 列表
            cancelled_leases = [
                lid for lid, lease in self._active_leases.items()
                if lease.worker_id == worker_id and lease.cancelled
            ]

            return True, {
                "status": "ack",
                "cancelled_leases": cancelled_leases
            }

    def get_online_devices(self) -> List[Dict[str, Any]]:
        """从已建立真实连接并保活的 Worker 获取设备列表 (C03: 真实保留设备 offline 状态，绝不伪造在线)。"""
        now = time.time()
        online_devices = []

        with self._lock:
            for wid, w_info in list(self._workers.items()):
                is_worker_online = (now - w_info["last_heartbeat"] <= self.heartbeat_timeout)
                if not is_worker_online:
                    continue

                for dev in w_info.get("devices", []):
                    dev_status = dev.get("status", "unknown")
                    # 保留 Worker 真实上报的设备状态，绝不强制覆盖为 online (C03)
                    online_devices.append({
                        **dev,
                        "worker_id": wid,
                        "worker_online": True,
                        "status": dev_status,
                        "status_text": (
                            f"🟢 物理设备就绪 (Worker: {wid})" if dev_status == "online"
                            else f"🔴 物理设备未连接/离线 ({dev_status}, Worker: {wid})"
                        )
                    })

        return online_devices

    def has_online_worker_for_device(self, device_id: str) -> bool:
        """检查指定设备是否在物理和 Worker 通道两层均处于在线就绪状态 (C03)。"""
        now = time.time()
        with self._lock:
            for wid, w_info in self._workers.items():
                if now - w_info["last_heartbeat"] <= self.heartbeat_timeout:
                    for dev in w_info.get("devices", []):
                        if dev.get("device_id") == device_id and dev.get("status") == "online":
                            return True
        return False

    def poll_task(
        self,
        worker_id: str,
        session_id: str,
        timeout_sec: float = 5.0
    ) -> Tuple[bool, Dict[str, Any]]:
        """Worker 领取任务租约 (C01 & C03: 严格会话校验、安全等待上限与仅派发在线设备)。"""
        timeout_bounded = min(max(float(timeout_sec), 0.5), 25.0)
        deadline = time.monotonic() + timeout_bounded

        with self._cond:
            ok, err = self._verify_session_locked(worker_id, session_id)
            if not ok:
                return False, {"error": err}

            worker = self._workers[worker_id]
            # 仅包含当前真实状态为 online 的设备 (C03)
            worker_online_device_ids = {
                d.get("device_id") for d in worker.get("devices", [])
                if d.get("status") == "online"
            }

            while not self._closed:
                # 查找适合该 Worker 且设备在线的任务
                for i, lease in enumerate(self._pending_leases):
                    if lease.device_id in worker_online_device_ids and not lease.cancelled:
                        self._pending_leases.pop(i)
                        lease.worker_id = worker_id
                        self._active_leases[lease.lease_id] = lease
                        logger.info(f"Worker '{worker_id}' 领取租约 '{lease.lease_id}' (Task: {lease.task_id})")
                        return True, {
                            "has_task": True,
                            "lease_id": lease.lease_id,
                            "task": lease.task_data,
                            "batch": lease.batch_data
                        }

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)

            return True, {"has_task": False}

    def submit_receipt(
        self,
        worker_id: str,
        session_id: str,
        lease_id: str,
        receipt: Dict[str, Any]
    ) -> Tuple[bool, Dict[str, Any]]:
        """Worker 上报执行回执 (C01: 严格会话认证与租约归属校验)。"""
        with self._lock:
            ok, err = self._verify_session_locked(worker_id, session_id)
            if not ok:
                return False, {"error": err}

            lease = self._active_leases.get(lease_id)
            if not lease:
                return False, {"error": f"租约 '{lease_id}' 不存在或已过期完成"}

            if lease.worker_id != worker_id:
                return False, {"error": "租约归属与上报 Worker 会话不匹配"}

            lease.receipt = receipt
            self._active_leases.pop(lease_id, None)
            lease.done_event.set()
            logger.info(f"Worker '{worker_id}' 成功提交租约 '{lease_id}' 回执")
            return True, {"status": "recorded"}

    def cancel_lease(self, lease_id: str) -> bool:
        """取消指定租约 (D01: 保留活跃租约直到 Worker 停止确认)。"""
        with self._lock:
            # 1. 检查排队队列中的租约 (尚未派发给 Worker，可立即取消)
            for i, l in enumerate(self._pending_leases):
                if l.lease_id == lease_id:
                    lease = self._pending_leases.pop(i)
                    lease.cancelled = True
                    lease.receipt = {
                        "status": "cancelled",
                        "warnings": ["任务在排队期间已被取消"]
                    }
                    lease.done_event.set()
                    return True

            # 2. 检查活跃执行中的租约 (Worker 正在物理执行，必须保留在 _active_leases 直至心跳下发中断并上报回执)
            lease = self._active_leases.get(lease_id)
            if lease:
                lease.cancelled = True
                # 注意：保留在 _active_leases 中，不立即 pop 也不提前 set done_event
                return True

            return False

    def cancel_lease_by_batch(self, batch_id: str) -> int:
        """根据 batch_id 取消在途和排队中的所有租约 (C03 & D01)。"""
        cancelled_count = 0
        with self._lock:
            # 活跃执行中租约：标记取消但保留在 _active_leases，等待 Worker 确认停止并上报
            for lease in self._active_leases.values():
                if lease.batch_data.get("batch_id") == batch_id:
                    lease.cancelled = True
                    cancelled_count += 1

            # 排队中租约：尚未派发至 Worker，直接弹出并结束
            pending_remaining = []
            for lease in self._pending_leases:
                if lease.batch_data.get("batch_id") == batch_id:
                    lease.cancelled = True
                    lease.receipt = {
                        "status": "cancelled",
                        "warnings": ["批次在排队阶段已被取消"]
                    }
                    lease.done_event.set()
                    cancelled_count += 1
                else:
                    pending_remaining.append(lease)
            self._pending_leases = pending_remaining

        return cancelled_count

    def has_active_lease_for_device(self, device_id: str) -> bool:
        """检查设备是否有在途未确认停止的活跃租约 (D01)。"""
        with self._lock:
            for lease in self._active_leases.values():
                if lease.device_id == device_id:
                    return True
        return False

    def dispatch_task_sync(
        self,
        device_id: str,
        task: Dict[str, Any],
        batch: Dict[str, Any],
        timeout_seconds: float = 180.0
    ) -> Dict[str, Any]:
        """中枢任务同步派发：创建租约，等待 Worker 领取并返回回执。"""
        if not self.has_online_worker_for_device(device_id):
            raise RuntimeError(f"无可用执行端或 Worker 离线 (VPS1 ↔ Mac 链路未建立，设备 '{device_id}' 无在线通道)")

        lease_id = f"lease-{uuid.uuid4().hex[:12]}"
        attempt_id = task["attempt_id"]

        lease = TaskLease(
            lease_id=lease_id,
            device_id=device_id,
            task_id=task["task_id"],
            attempt_id=attempt_id,
            task_data=task,
            batch_data=batch,
            ttl_seconds=timeout_seconds
        )

        with self._cond:
            self._pending_leases.append(lease)
            self._cond.notify_all()

        # 等待回执
        finished = lease.done_event.wait(timeout=timeout_seconds)
        if not finished:
            self.cancel_lease(lease_id)
            return {
                "schema_version": "1.0.0",
                "batch_id": task["batch_id"],
                "task_id": task["task_id"],
                "attempt_id": attempt_id,
                "app_id": task["app_id"],
                "status": "timed_out",
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "partial": False,
                "items": [],
                "warnings": [f"中枢租约等待超时 ({timeout_seconds}s)，在途任务已标记取消等待 Worker 停止确认"],
                "reset_status": "unverified",
                "timings": {"prepare_ms": 0, "search_ms": 0, "details_ms": 0, "restore_ms": 0, "total_ms": int(timeout_seconds * 1000)}
            }

        if lease.cancelled and not lease.receipt:
            return {
                "schema_version": "1.0.0",
                "batch_id": task["batch_id"],
                "task_id": task["task_id"],
                "attempt_id": attempt_id,
                "app_id": task["app_id"],
                "status": "cancelled",
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "partial": False,
                "items": [],
                "warnings": ["任务在中枢已被主动取消"],
                "reset_status": "unverified",
                "timings": {"prepare_ms": 0, "search_ms": 0, "details_ms": 0, "restore_ms": 0, "total_ms": 0}
            }

        return lease.receipt or {
            "status": "execution_unknown",
            "warnings": ["Worker 回执为空"]
        }
