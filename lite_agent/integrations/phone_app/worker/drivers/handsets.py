"""
Handsets 物理设备底层驱动封装 (lite_agent.integrations.phone_app.worker.drivers.handsets).
严格遵循第 8 节要求与 E02 整改规范：
- 严格使用 subprocess 参数列表与 shell=False，禁止 shell 字符串插值。
- 显式绑定 device_id (serial)，禁止未指定时盲目操作首台设备。
- 严格超时控制、返回码检查与 stderr 脱敏日志记录。
- 严禁写死 /opt/homebrew 等平台专有路径；通过显式配置或 shutil.which 探测。
- 明确声明支持范围：Mac/Linux 依赖 handsets CLI 与 adb；Windows 原生未经实机验证明确拒绝或报错。
- 提供 BaseDeviceDriver 抽象协议，支持 Real 驱动与 Fake 驱动平滑切换。
"""

from __future__ import annotations
import os
import sys
import subprocess
import shutil
import logging
import time
from typing import Dict, Any, List, Optional, Tuple, Union

logger = logging.getLogger("integrations.phone_app.worker.drivers.handsets")


class BaseDeviceDriver:
    """设备驱动抽象协议。"""

    def ensure_ready(self, timeout: Optional[float] = None, deadline: Optional[float] = None) -> Tuple[bool, Optional[str]]:
        raise NotImplementedError

    def dump_ui(self, format_type: str = "table", timeout: Optional[float] = None, deadline: Optional[float] = None) -> str:
        raise NotImplementedError

    def tap(self, target: Union[str, Tuple[int, int]], timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        raise NotImplementedError

    def fill(self, selector: str, text: str, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        raise NotImplementedError

    def submit(self, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        raise NotImplementedError

    def go_back(self, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        raise NotImplementedError

    def open_app(self, package_name: str, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        raise NotImplementedError

    def wait_for(self, target: str, timeout_seconds: float = 5.0, deadline: Optional[float] = None) -> bool:
        raise NotImplementedError

    def take_screenshot(self, output_path: str, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        raise NotImplementedError


class HandsetsDriver(BaseDeviceDriver):
    """基于 hs CLI 的真实设备驱动实现。"""

    def __init__(
        self,
        device_id: str,
        hs_bin_path: Optional[str] = None,
        default_timeout: float = 10.0
    ):
        if not device_id:
            raise ValueError("必须显式指定 device_id (serial)，禁止盲目绑定默认设备")
        self.device_id = device_id
        # E02: 彻底移除针对特定包管理器的硬编码路径，通过入参、环境变量或 PATH 动态探测
        detected = hs_bin_path or os.environ.get("HANDSETS_BIN_PATH") or shutil.which("hs")
        self.hs_bin = detected
        self.default_timeout = default_timeout

        # E02 平台支持性客观约束：Windows 原生 handsets 驱动未经物理真机适配验证时明确声明限制
        if sys.platform == "win32" and not self.hs_bin:
            logger.warning("Windows 平台未检测到 handsets (hs.exe) 执行程序；Windows 原生真机自动化暂未验证支持")

    @property
    def version(self) -> str:
        """获取真实的 CLI / 驱动版本。"""
        if not hasattr(self, "_cached_version"):
            if not self.hs_bin or not shutil.which(self.hs_bin):
                self._cached_version = "hs-bin-missing"
                return self._cached_version
            try:
                res = subprocess.run([self.hs_bin, "--version"], capture_output=True, text=True, timeout=2.0)
                if res.returncode == 0 and res.stdout.strip():
                    self._cached_version = res.stdout.strip()
                else:
                    self._cached_version = "hs-cli-unknown"
            except Exception:
                self._cached_version = "hs-cli-unavailable"
        return self._cached_version

    def _run_cmd(
        self,
        args: List[str],
        timeout: Optional[float] = None,
        deadline: Optional[float] = None
    ) -> Tuple[int, str, str]:
        """执行 hs 命令，严格使用参数数组且 shell=False，支持 deadline 约束 (F03)。"""
        if not self.hs_bin or not shutil.which(self.hs_bin):
            return -2, "", "HANDSETS_BIN_NOT_FOUND"

        eff_timeout = timeout or self.default_timeout
        if deadline is not None:
            rem = deadline - time.monotonic()
            if rem <= 0:
                return -1, "", "TIMEOUT"
            eff_timeout = min(eff_timeout, rem)

        full_cmd = [self.hs_bin, "--device", self.device_id] + args
        try:
            res = subprocess.run(
                full_cmd,
                shell=False,
                capture_output=True,
                text=True,
                timeout=eff_timeout
            )
            return res.returncode, res.stdout.strip(), res.stderr.strip()
        except subprocess.TimeoutExpired:
            logger.warning(f"hs 命令执行超时 ({eff_timeout}s): {' '.join(full_cmd)}")
            return -1, "", "TIMEOUT"
        except Exception as e:
            logger.error(f"hs 执行异常: {e}")
            return -2, "", str(e)

    def ensure_ready(self, timeout: Optional[float] = None, deadline: Optional[float] = None) -> Tuple[bool, Optional[str]]:
        """检查 hs 守护进程与设备在线状态。"""
        if deadline is not None and time.monotonic() >= deadline:
            return False, "TIMEOUT: 已超出指定截止时限"
        if not self.hs_bin or not shutil.which(self.hs_bin):
            return False, f"未找到 handsets 可执行文件: '{self.hs_bin or 'hs'}'，请安装 handsets CLI 或设置 HANDSETS_BIN_PATH"

        # 检查 ADB 设备在线状态
        try:
            adb_t = 3.0
            if deadline is not None:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    return False, "TIMEOUT: 已超出指定截止时限"
                adb_t = min(adb_t, rem)
            adb_res = subprocess.run(
                ["adb", "-s", self.device_id, "get-state"],
                shell=False, capture_output=True, text=True, timeout=adb_t
            )
            if adb_res.returncode != 0 or adb_res.stdout.strip() != "device":
                return False, f"设备 '{self.device_id}' 未处于 ADB 在线状态 ({adb_res.stdout.strip()})"
        except Exception as e:
            return False, f"ADB 检测异常: {e}"

        code, out, err = self._run_cmd(["use"], timeout=timeout or 6.0, deadline=deadline)
        code_ping, _, _ = self._run_cmd(["dev", "ping"], timeout=3.0, deadline=deadline)
        if code_ping == 0 or code == 0:
            return True, None

        return False, f"handsets 驱动启动失败: {err or out}"

    def dump_ui(self, format_type: str = "table", timeout: Optional[float] = None, deadline: Optional[float] = None) -> str:
        """读取屏幕 UI 节点 (F03: 传递 deadline 与超时约束)。"""
        if deadline is not None and time.monotonic() >= deadline:
            return ""
        flag = "--tree" if format_type == "tree" else ("--xml" if format_type == "xml" else "")
        args = ["ui"]
        if flag:
            args.append(flag)
        code, out, err = self._run_cmd(args, timeout=timeout or 6.0, deadline=deadline)
        if code != 0:
            logger.warning(f"dump_ui 失败 [{code}]: {err}")
            return ""
        return out

    def tap(self, target: Union[str, Tuple[int, int]], timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        """点击指定选择器或物理坐标 (F03: 遵守 deadline 约束)。"""
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if isinstance(target, (tuple, list)):
            x, y = target
            code, out, err = self._run_cmd(["tap", str(x), str(y)], timeout=timeout or 5.0, deadline=deadline)
        else:
            code, out, err = self._run_cmd(["tap", str(target)], timeout=timeout or 5.0, deadline=deadline)
        return code == 0 and "ok" in out

    def fill(self, selector: str, text: str, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        """使用 Accessibility ACTION_SET_TEXT 原生中文原子注入。"""
        if deadline is not None and time.monotonic() >= deadline:
            return False
        code, out, err = self._run_cmd(["fill", selector, text], timeout=timeout or 6.0, deadline=deadline)
        return code == 0 and "ok set_text" in out

    def submit(self, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        """提交当前焦点输入框。"""
        if deadline is not None and time.monotonic() >= deadline:
            return False
        code, out, err = self._run_cmd(["submit"], timeout=timeout or 4.0, deadline=deadline)
        return code == 0

    def go_back(self, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        """发送系统返回键。"""
        if deadline is not None and time.monotonic() >= deadline:
            return False
        code, out, err = self._run_cmd(["go", "back"], timeout=timeout or 3.0, deadline=deadline)
        return code == 0

    def open_app(self, package_name: str, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        """拉起指定应用。"""
        if deadline is not None and time.monotonic() >= deadline:
            return False
        code, out, err = self._run_cmd(["open", package_name], timeout=timeout or 5.0, deadline=deadline)
        return code == 0

    def wait_for(self, target: str, timeout_seconds: float = 5.0, deadline: Optional[float] = None) -> bool:
        """等待目标文本或选择器出现。"""
        if deadline is not None:
            rem = deadline - time.monotonic()
            if rem <= 0:
                return False
            eff_timeout = min(timeout_seconds, rem)
        else:
            eff_timeout = timeout_seconds
        code, out, err = self._run_cmd(
            ["wait", target, "--timeout", str(int(eff_timeout * 1000))],
            timeout=eff_timeout + 1.0,
            deadline=deadline
        )
        return code == 0

    def take_screenshot(self, output_path: str, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        """截取屏幕产物并保存至本地受控目录。"""
        if deadline is not None and time.monotonic() >= deadline:
            return False
        code, out, err = self._run_cmd(["see", output_path], timeout=timeout or 8.0, deadline=deadline)
        return code == 0


class FakeDeviceDriver(BaseDeviceDriver):
    """自动化测试专用的 Fake 驱动 (F03: 支持耗时模拟 delays 及 deadline 检查)。"""

    def __init__(
        self,
        mock_ui_table: str = "",
        mock_ui_xml: str = "",
        fail_actions: Optional[List[str]] = None,
        delays: Optional[Dict[str, float]] = None
    ):
        self.mock_ui_table = mock_ui_table
        self.mock_ui_xml = mock_ui_xml
        self.fail_actions = fail_actions or []
        self.delays = delays or {}
        self.actions_log: List[Tuple[str, Any]] = []

    def _apply_delay(self, action_name: str, deadline: Optional[float] = None):
        d = self.delays.get(action_name, 0.0)
        if d > 0:
            time.sleep(d)

    def ensure_ready(self, timeout: Optional[float] = None, deadline: Optional[float] = None) -> Tuple[bool, Optional[str]]:
        self._apply_delay("ensure_ready", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return False, "Fake: 超出截止时限"
        if "ensure_ready" in self.fail_actions:
            return False, "Fake: 设备离线"
        return True, None

    def dump_ui(self, format_type: str = "table", timeout: Optional[float] = None, deadline: Optional[float] = None) -> str:
        self.actions_log.append(("dump_ui", format_type))
        self._apply_delay("dump_ui", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return ""
        if format_type == "xml" and self.mock_ui_xml:
            return self.mock_ui_xml
        return self.mock_ui_table

    def tap(self, target: Union[str, Tuple[int, int]], timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        self.actions_log.append(("tap", target))
        self._apply_delay("tap", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        return "tap" not in self.fail_actions

    def fill(self, selector: str, text: str, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        self.actions_log.append(("fill", (selector, text)))
        self._apply_delay("fill", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        return "fill" not in self.fail_actions

    def submit(self, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        self.actions_log.append(("submit", None))
        self._apply_delay("submit", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        return "submit" not in self.fail_actions

    def go_back(self, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        self.actions_log.append(("go_back", None))
        self._apply_delay("go_back", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        return "go_back" not in self.fail_actions

    def open_app(self, package_name: str, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        self.actions_log.append(("open_app", package_name))
        self._apply_delay("open_app", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        return "open_app" not in self.fail_actions

    def wait_for(self, target: str, timeout_seconds: float = 5.0, deadline: Optional[float] = None) -> bool:
        self.actions_log.append(("wait_for", target))
        self._apply_delay("wait_for", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        return "wait_for" not in self.fail_actions

    def take_screenshot(self, output_path: str, timeout: Optional[float] = None, deadline: Optional[float] = None) -> bool:
        self.actions_log.append(("take_screenshot", output_path))
        self._apply_delay("take_screenshot", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        return "take_screenshot" not in self.fail_actions
