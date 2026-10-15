"""
应用生命周期与资源清理管理器 (app.lifecycle)。
负责资源的按序登记、回滚、优雅关闭与所有权释放。
"""

import logging
from typing import Callable, List, Tuple

logger = logging.getLogger("app.lifecycle")


class LifecycleManager:
    """生命周期管理器。管理连接、作业、通道等资源的注册与关闭释放。"""

    def __init__(self):
        self._resources: List[Tuple[str, Callable[[], None]]] = []
        self._is_shutdown = False

    @property
    def is_shutdown(self) -> bool:
        return self._is_shutdown

    def register_resource(self, name: str, cleanup_fn: Callable[[], None]) -> None:
        """登记一个受管资源及其清理函数。"""
        if self._is_shutdown:
            raise RuntimeError(f"应用正在关闭或已关闭，无法登记新资源: {name}")
        self._resources.append((name, cleanup_fn))

    def shutdown(self) -> List[str]:
        """按注册倒序逆序释放所有受管资源，即使单个资源失败也继续清理其余资源。"""
        self._is_shutdown = True
        failed_cleanups: List[str] = []

        for name, cleanup_fn in reversed(self._resources):
            try:
                cleanup_fn()
            except Exception as e:
                logger.warning(f"释放资源失败 [{name}]: {e}")
                failed_cleanups.append(f"{name}: {e}")

        self._resources.clear()
        return failed_cleanups
