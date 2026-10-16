"""
定时任务引擎 (core.cron_engine)。
已重构并委托至 services.cron.CronService，保持接口与单例访问完全向后兼容。
"""

from services.cron import CronJob, CronService, default_cron_service
import threading


class CronManager(CronService):
    """向后兼容的 CronManager 单例包装。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(CronManager, cls).__new__(cls)
                cls._instance._is_initialized = False
            return cls._instance

    def __init__(self, *args, **kwargs):
        if getattr(self, "_is_initialized", False):
            return
        super().__init__(*args, **kwargs)
        self._is_initialized = True


__all__ = ["CronJob", "CronManager", "CronService", "default_cron_service"]
