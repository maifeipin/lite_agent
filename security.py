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
        """
        检查路径是否在沙箱范围内。
        注意：
        1. 当 sandbox_paths 为空时，保持现有开放语义（返回 True），注意这不构成沙箱隔离；
        2. 当配置了 sandbox_paths 时，以 realpath 与 commonpath 判定是否在允许根目录下；
        3. 对于尚未创建的目标路径，递归解析其存在的父目录 realpath 再做判定；
        4. 本函数属于预检查机制，无法消除并发重命名/符号链接切换引起的 TOCTOU，不能替代 OS 级沙箱隔离。
        """
        if not self.sandbox_paths:
            return True
        if not path or not isinstance(path, str):
            return False

        try:
            target_path = os.path.abspath(path)
            curr = target_path
            components = []
            while not os.path.lexists(curr):
                parent, name = os.path.split(curr)
                if not name or parent == curr:
                    break
                components.append(name)
                curr = parent

            real_base = os.path.realpath(curr)
            if components:
                components.reverse()
                real_target = os.path.normpath(os.path.join(real_base, *components))
            else:
                real_target = real_base

            for sp in self.sandbox_paths:
                if not sp or not isinstance(sp, str):
                    continue
                real_root = os.path.realpath(os.path.abspath(sp))
                try:
                    common = os.path.commonpath([real_root, real_target])
                    if common == real_root:
                        return True
                except ValueError:
                    continue
            return False
        except Exception:
            return False

    def check_command(self, cmd: str) -> tuple:
        """
        检查命令是否安全
        :return: (is_safe: bool, reason: str)
        """
        for blocked in self.blocked_commands:
            if blocked in cmd:
                return False, f"命令包含被禁止的操作: {blocked}"
        return True, ""
