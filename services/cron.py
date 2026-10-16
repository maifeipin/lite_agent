"""
系统定时作业服务 (services.cron)。
提供实例隔离、重名检查、动态注销与优雅启停，避免全局单例污染与后台残留线程。
"""

import time
import threading
import traceback
import re
from datetime import datetime
from typing import Callable, Dict, Optional, Any, List
from concurrent.futures import ThreadPoolExecutor


class CronJob:
    """单个定时作业契约与执行状态。"""

    def __init__(self, job_id: int, name: str, cron_expr: str, func: Callable, enabled: bool = True):
        self.id = job_id
        self.name = name
        self.cron_expr = cron_expr.strip() if isinstance(cron_expr, str) else ""
        self._minutes, self._hours = self._parse_schedule(self.cron_expr)
        self.func = func
        self.enabled = enabled
        self.last_run_date = ""
        self._job_lock = threading.Lock()
        self.is_running = False
        self._schedule_lock = threading.Lock()
        self._last_run_minute = None

    @staticmethod
    def _parse_schedule(expr: str):
        if expr == "every_minute":
            return set(range(60)), set(range(24))
        daily = re.fullmatch(r"(\d{1,2}):(\d{2})", expr)
        if daily and int(daily[1]) < 24 and int(daily[2]) < 60:
            return {int(daily[2])}, {int(daily[1])}
        fields = expr.split()
        if len(fields) == 5 and fields[2:] == ["*", "*", "*"]:
            def values(field, maximum):
                if field == "*":
                    return set(range(maximum + 1))
                if re.fullmatch(r"\d+", field) and int(field) <= maximum:
                    return {int(field)}
                if re.fullmatch(r"\*/\d+", field):
                    interval = int(field[2:])
                    if 1 <= interval <= maximum:
                        return set(range(0, maximum + 1, interval))
                raise ValueError(f"Unsupported cron field: {field!r}")
            return values(fields[0], 59), values(fields[1], 23)
        raise ValueError(f"Unsupported cron expression: {expr!r}; use HH:MM, every_minute, or minute hour * * *")

    def should_run(self, current_time: datetime) -> bool:
        if not self.enabled:
            return False
        if current_time.minute not in self._minutes or current_time.hour not in self._hours:
            return False
        minute_key = int(current_time.timestamp() // 60)
        with self._schedule_lock:
            if self._last_run_minute is not None and minute_key <= self._last_run_minute:
                return False
            self._last_run_minute = minute_key
            self.last_run_date = current_time.strftime("%Y-%m-%d")
            return True


class CronService:
    """定时作业服务。支持独立实例化，具备重名检查、动态注销与无残留启停。"""

    def __init__(self, log_callback: Optional[Callable] = None):
        self.jobs: Dict[int, CronJob] = {}
        self._job_names: Dict[str, int] = {}
        self._handlers: Dict[str, Callable] = {}
        self._next_id = 1
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.executor: Optional[ThreadPoolExecutor] = None
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._log_callback = log_callback or (lambda msg: None)

    _init = __init__

    def set_log_callback(self, callback: Callable):
        self._log_callback = callback

    def add_job(self, name: str, cron_expr: str, func: Callable, enabled: bool = True) -> int:
        """添加一个定时任务。重名抛出 ValueError。"""
        with self._lock:
            if name in self._job_names:
                raise ValueError(f"定时任务已存在，禁止重复注册: {name}")
            job_id = self._next_id
            job = CronJob(job_id, name, cron_expr, func, enabled)
            self._next_id += 1
            self.jobs[job_id] = job
            self._job_names[name] = job_id
            return job_id

    def remove_job_by_name(self, name: str) -> bool:
        """根据名称注销定时任务。用于插件卸载或生命周期回滚。"""
        with self._lock:
            job_id = self._job_names.pop(name, None)
            if job_id is not None:
                self.jobs.pop(job_id, None)
                return True
            return False

    def remove_job(self, job_id: int) -> bool:
        """根据 ID 注销定时任务。"""
        with self._lock:
            job = self.jobs.pop(job_id, None)
            if job:
                self._job_names.pop(job.name, None)
                return True
            return False

    def register_handler(self, name: str, handler: Callable):
        """登记专用作业回调处理器 (如 ops_rss::rss_push, ops_self_check::daily_health)。"""
        with self._lock:
            if name in self._handlers:
                raise ValueError(f"作业处理器已存在，禁止重复注册: {name}")
            self._handlers[name] = handler

    def unregister_handler(self, name: str) -> bool:
        """注销专用作业回调处理器。"""
        with self._lock:
            return self._handlers.pop(name, None) is not None

    def get_handler(self, name: str) -> Optional[Callable]:
        """获取指定名称的专用作业回调处理器。"""
        with self._lock:
            return self._handlers.get(name)

    def clear(self):
        """清空所有定时任务与作业处理器。"""
        with self._lock:
            self.jobs.clear()
            self._job_names.clear()
            self._handlers.clear()

    def list_jobs(self) -> str:
        with self._lock:
            if not self.jobs:
                return "当前没有任何定时任务。"
            lines = ["📅 **系统定时任务列表**"]
            for job_id, job in self.jobs.items():
                status = "✅ 开启" if job.enabled else "⏸️ 暂停"
                lines.append(f"[{job.id}] {status} | **{job.name}** (时间: {job.cron_expr})")
            lines.append("\n💡 提示: 发送 `/cron <序号>` 手动执行，发送 `/cron toggle <序号>` 开启或暂停任务。")
            return "\n".join(lines)

    def toggle_job(self, job_id: int) -> str:
        with self._lock:
            if job_id not in self.jobs:
                return f"❌ 找不到序号为 {job_id} 的任务。"
            job = self.jobs[job_id]
            job.enabled = not job.enabled
            state = "已开启" if job.enabled else "已暂停"
            return f"✅ 任务 [{job.id}] {job.name} {state}。"

    def run_job_manually(self, job_id: int) -> str:
        with self._lock:
            if job_id not in self.jobs:
                return f"❌ 找不到序号为 {job_id} 的任务。"
            job = self.jobs[job_id]
        try:
            result = job.func()
            return f"🚀 手动执行 [{job.name}] 成功:\n{result}"
        except Exception as e:
            err_msg = traceback.format_exc()
            return f"❌ 手动执行 [{job.name}] 失败: {e}\n{err_msg}"

    def start(self):
        with self._lock:
            if self.running:
                return
            self.running = True
            self.executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="CronWorker")
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="CronEngine")
            self._thread.start()

    def stop(self, wait: bool = True, timeout: float = 3.0):
        """停止调度引擎，释放线程与后台资源。"""
        with self._lock:
            if not self.running:
                return
            self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        if self.executor:
            try:
                self.executor.shutdown(wait=wait, cancel_futures=True)
            except TypeError:
                self.executor.shutdown(wait=wait)
            except Exception:
                pass
            self.executor = None

    def _execute_job_task(self, job: CronJob):
        with job._job_lock:
            if job.is_running:
                return
            job.is_running = True
        try:
            with self._log_lock:
                self._log_callback(f"🕒 正在自动执行定时任务: {job.name}...")
            res = job.func()
            if res:
                with self._log_lock:
                    self._log_callback(f"✅ 定时任务 [{job.name}] 执行完毕:\n{res}")
        except Exception as e:
            err_msg = traceback.format_exc()
            with self._log_lock:
                self._log_callback(f"❌ 定时任务 [{job.name}] 执行失败: {e}\n{err_msg}")
        finally:
            with job._job_lock:
                job.is_running = False

    def _run_loop(self):
        while self.running:
            now = datetime.now()
            with self._lock:
                jobs_to_run = [job for job in self.jobs.values() if job.should_run(now)]
            for job in jobs_to_run:
                try:
                    if self.executor:
                        self.executor.submit(self._execute_job_task, job)
                except RuntimeError:
                    pass
            # 睡眠 1 秒检测停止，避免长 sleep 导致无法退出
            for _ in range(60 - datetime.now().second):
                if not self.running:
                    break
                time.sleep(1)


default_cron_service = CronService()
