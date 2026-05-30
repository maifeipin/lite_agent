"""
安全层 - 用户白名单、路径沙箱、危险命令拦截
"""

import os
import re


class SecurityGuard:
    """
    安全守卫
    - 白名单模式: 空列表 = 允许所有人
    - 沙箱路径: 限制文件读取范围
    - 命令黑名单: 拦截危险 Shell 操作
    """

    def __init__(self, config: dict):
        sec_cfg = config.get("security", {})
        self.allowed_users = set(sec_cfg.get("allowed_users", []))
        self.sandbox_paths = sec_cfg.get("sandbox_paths", [])
        self.blocked_commands = sec_cfg.get("blocked_commands", [])

    def check_user(self, channel: str, user_id: str) -> bool:
        """检查用户是否在白名单中 (空白名单 = 允许所有人)"""
        if not self.allowed_users:
            return True
        return (f"{channel}:{user_id}" in self.allowed_users
                or user_id in self.allowed_users)

    def check_path(self, path: str) -> bool:
        """检查路径是否在沙箱范围内"""
        if not self.sandbox_paths:
            return True
        abs_path = os.path.abspath(path)
        return any(
            abs_path.startswith(os.path.abspath(sp))
            for sp in self.sandbox_paths
        )

    def check_command(self, cmd: str) -> tuple:
        """
        检查命令是否安全
        :return: (is_safe: bool, reason: str)
        """
        for blocked in self.blocked_commands:
            if blocked in cmd:
                return False, f"命令包含被禁止的操作: {blocked}"
        return True, ""
