"""
设备离线原因贯通链路测试 (test_phone_app_device_diagnostics.py).

覆盖 C03 "离线必须可诊断" 要求：
驱动层失败原因 → 稳定错误码 → Worker 上报载荷 → 中枢白名单透传 → 可读 status_text。
"""

import subprocess
import sys

import pytest

from lite_agent.integrations.phone_app.worker.drivers import handsets as handsets_module
from lite_agent.integrations.phone_app.worker.drivers.handsets import (
    BaseDeviceDriver,
    HandsetsDriver,
    classify_ready_error,
    format_ready_reason,
)
from lite_agent.integrations.phone_app.worker.worker import MacWorkerClient
from lite_agent.features.phone_app.worker_hub import WorkerHub


# --- 驱动层: 原因归类 ---

@pytest.mark.parametrize(
    "reason, expected",
    [
        ("未找到 handsets 可执行文件: 'hs'，请安装 handsets CLI 或设置 HANDSETS_BIN_PATH", "hs_missing"),
        ("设备 'c68a3cf2' 未处于 ADB 在线状态 ()", "adb_no_device"),
        ("设备 'c68a3cf2' 未处于 ADB 在线状态 (unauthorized)", "adb_unauthorized"),
        ("ADB 检测异常: [Errno 2] No such file or directory: 'adb'", "adb_missing"),
        ("ADB 检测异常: OSError: device busy", "adb_error"),
        (
            "handsets 驱动启动失败: error: --device c68a3cf2: no forwarded daemon "
            "— run `hs use c68a3cf2` first",
            "hs_daemon_unbound",
        ),
        ("handsets 驱动启动失败: hs: permission denied", "hs_driver_error"),
        ("TIMEOUT: 已超出指定截止时限", "timeout"),
        ("ADB 检测超时: adb get-state 未在 3.0s 内返回", "timeout"),
        ("ADB 检测异常: 未找到 adb 可执行文件 ([Errno 2] No such file or directory: 'adb')", "adb_missing"),
        ("设备 'c68a3cf2' 未处于 ADB 在线状态 ( | error: device unauthorized.)", "adb_unauthorized"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_classify_ready_error_maps_each_failure_stage(reason, expected):
    """五类真实故障必须归到不同错误码，不得一律 unknown。"""
    assert classify_ready_error(reason) == expected


def test_format_ready_reason_collapses_and_truncates():
    """hs 报错常附带整段 usage 帮助，上报前必须折叠换行并截断。"""
    raw = "handsets 驱动启动失败: error: no forwarded daemon\n\nUsage:\n  hs use\n" + "x" * 500
    out = format_ready_reason(raw, limit=80)
    assert "\n" not in out
    assert len(out) <= 81
    assert out.endswith("…")
    assert format_ready_reason("") == ""


# --- 客户端层: check_online 保留原因 ---

class _FailingDriver(BaseDeviceDriver):
    def __init__(self, reason):
        self._reason = reason

    def ensure_ready(self, timeout=None, deadline=None):
        return False, self._reason


class _ReadyDriver(BaseDeviceDriver):
    def ensure_ready(self, timeout=None, deadline=None):
        return True, None


def test_check_online_keeps_error_code_and_reason():
    """旧实现 is_online() 直接丢弃 reason，导致前端只能看到 offline。"""
    client = MacWorkerClient(device_id="c68a3cf2", driver=_FailingDriver(
        "handsets 驱动启动失败: error: no forwarded daemon — run `hs use c68a3cf2` first"
    ))
    ok, code, reason = client.check_online()
    assert ok is False
    assert code == "hs_daemon_unbound"
    assert "no forwarded daemon" in reason
    # is_online 保持向后兼容
    assert client.is_online() is False


def test_check_online_returns_empty_diagnostics_when_ready():
    client = MacWorkerClient(device_id="c68a3cf2", driver=_ReadyDriver())
    assert client.check_online() == (True, "", "")


# --- 中枢层: 白名单透传与可读文案 ---

def _handshake(hub, devices):
    ok, res = hub.handshake(
        worker_id="mac-worker-oneplus9",
        token="test-token",
        version="1.0.0",
        capabilities=["jd"],
        devices=devices,
    )
    assert ok is True
    return res["session_id"]


def test_hub_preserves_offline_reason_and_renders_label():
    hub = WorkerHub(expected_token="test-token")
    _handshake(hub, [
        {
            "device_id": "c68a3cf2",
            "model": "OnePlus 9 (LE2110)",
            "status": "offline",
            "error_code": "adb_no_device",
            "offline_reason": "设备 'c68a3cf2' 未处于 ADB 在线状态 ()",
        }
    ])

    devs = hub.get_online_devices()
    assert len(devs) == 1
    dev = devs[0]
    # C03: 状态不得被篡改为 online
    assert dev["status"] == "offline"
    assert dev["error_code"] == "adb_no_device"
    assert "ADB" in dev["offline_reason"]
    assert "ADB 未识别到设备" in dev["status_text"]
    assert "🔴" in dev["status_text"]


def test_hub_heartbeat_updates_reason_and_keeps_whitelist():
    hub = WorkerHub(expected_token="test-token")
    session_id = _handshake(hub, [{"device_id": "c68a3cf2", "status": "online"}])

    ok, _ = hub.heartbeat(
        worker_id="mac-worker-oneplus9",
        session_id=session_id,
        devices=[
            {
                "device_id": "c68a3cf2",
                "status": "offline",
                "error_code": "hs_daemon_unbound",
                "offline_reason": "no forwarded daemon — run `hs use c68a3cf2` first",
                "injected_unknown_field": "must-be-dropped",
            }
        ],
    )
    assert ok is True

    dev = hub.get_online_devices()[0]
    assert dev["status"] == "offline"
    assert dev["error_code"] == "hs_daemon_unbound"
    assert "hs 守护进程未绑定设备" in dev["status_text"]
    # D04: 白名单外字段不得透传
    assert "injected_unknown_field" not in dev


def test_hub_online_device_has_no_offline_label():
    hub = WorkerHub(expected_token="test-token")
    _handshake(hub, [{"device_id": "c68a3cf2", "status": "online"}])

    dev = hub.get_online_devices()[0]
    assert dev["status"] == "online"
    assert "🟢" in dev["status_text"]
    assert "未识别" not in dev["status_text"]


# --- 真实驱动路径 (R2): 用 mock subprocess 驱动真实 ensure_ready，而非手工构造原因 ---

def _real_driver() -> HandsetsDriver:
    """hs_bin 指向真实存在的可执行文件，避免依赖本机是否安装 handsets。"""
    return HandsetsDriver(device_id="c68a3cf2", hs_bin_path=sys.executable)


def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["adb"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_real_ensure_ready_reads_adb_stderr_unauthorized(monkeypatch):
    """ADB 未授权信息只出现在 stderr 时，不得被误报为"未连接"。"""
    monkeypatch.setattr(
        handsets_module.subprocess,
        "run",
        lambda cmd, **kwargs: _completed(1, stdout="", stderr="error: device unauthorized."),
    )

    driver = _real_driver()
    ok, reason = driver.ensure_ready()

    assert ok is False
    assert "unauthorized" in reason, f"stderr 诊断信息被丢弃: {reason!r}"
    assert classify_ready_error(reason) == "adb_unauthorized"

    # 全链路: 驱动 → check_online → Worker 上报载荷
    client = MacWorkerClient(device_id="c68a3cf2", driver=driver)
    _, code, trimmed = client.check_online()
    assert code == "adb_unauthorized"
    assert "unauthorized" in trimmed


def test_real_ensure_ready_reports_adb_timeout_distinctly(monkeypatch):
    """ADB 自身超时必须单独归类，不得落到 adb_error 或 adb_no_device。"""

    def _raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 3.0))

    monkeypatch.setattr(handsets_module.subprocess, "run", _raise_timeout)

    driver = _real_driver()
    ok, reason = driver.ensure_ready()

    assert ok is False
    assert "超时" in reason
    assert classify_ready_error(reason) == "timeout"


def test_real_ensure_ready_reports_missing_adb_binary(monkeypatch):
    def _raise_missing(cmd, **kwargs):
        raise FileNotFoundError("adb")

    monkeypatch.setattr(handsets_module.subprocess, "run", _raise_missing)

    ok, reason = _real_driver().ensure_ready()
    assert ok is False
    assert classify_ready_error(reason) == "adb_missing"


def test_real_ensure_ready_reports_hs_daemon_unbound(monkeypatch):
    """adb 正常但 hs 未绑定设备时，必须归到 hs_daemon_unbound 而不是 adb_*。"""
    monkeypatch.setattr(
        handsets_module.subprocess,
        "run",
        lambda cmd, **kwargs: _completed(0, stdout="device\n", stderr=""),
    )

    driver = _real_driver()
    monkeypatch.setattr(
        driver,
        "_run_cmd",
        lambda *args, **kwargs: (1, "error: --device c68a3cf2: no forwarded daemon — run `hs use c68a3cf2` first", ""),
    )

    ok, reason = driver.ensure_ready()
    assert ok is False
    assert classify_ready_error(reason) == "hs_daemon_unbound"


def test_real_ensure_ready_online_path_is_clean(monkeypatch):
    """设备正常时不得误报离线，且载荷中诊断字段为空。"""
    monkeypatch.setattr(
        handsets_module.subprocess,
        "run",
        lambda cmd, **kwargs: _completed(0, stdout="device\n", stderr=""),
    )

    driver = _real_driver()
    monkeypatch.setattr(driver, "_run_cmd", lambda *args, **kwargs: (0, "ok", ""))

    ok, reason = driver.ensure_ready()
    assert ok is True and reason is None

    client = MacWorkerClient(device_id="c68a3cf2", driver=driver)
    assert client.check_online() == (True, "", "")


def test_timeout_reaches_device_payload_through_real_chain(monkeypatch):
    """超时错误必须一路走到 Worker 上报载荷，供中枢与前端展示。"""
    from lite_agent.integrations.phone_app.worker.run_worker import MacWorkerDaemon

    def _raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 3.0))

    monkeypatch.setattr(handsets_module.subprocess, "run", _raise_timeout)

    daemon = MacWorkerDaemon(hub_url="https://mail.example.com/agent", token="token-for-test", fake_mode=False)
    daemon.client = MacWorkerClient(device_id="c68a3cf2", driver=_real_driver())

    payload = daemon._device_payload()
    assert payload["status"] == "offline"
    assert payload["error_code"] == "timeout"
    assert "超时" in payload["offline_reason"]


# --- 上报层: daemon 载荷字段完整 ---

def test_daemon_device_payload_carries_diagnostics(monkeypatch):
    """握手/心跳载荷必须同时携带 status / error_code / offline_reason。"""
    from lite_agent.integrations.phone_app.worker.run_worker import MacWorkerDaemon

    daemon = MacWorkerDaemon(
        hub_url="https://mail.example.com/agent",
        token="token-for-test",
        fake_mode=False,
    )
    daemon.client = MacWorkerClient(
        device_id="c68a3cf2",
        driver=_FailingDriver("设备 'c68a3cf2' 未处于 ADB 在线状态 (unauthorized)"),
    )

    payload = daemon._device_payload()
    assert payload["device_id"] == "c68a3cf2"
    assert payload["status"] == "offline"
    assert payload["error_code"] == "adb_unauthorized"
    assert "unauthorized" in payload["offline_reason"]
    assert payload["test_mode"] is False
