import time
import threading
import traceback
import re
from datetime import datetime
from typing import Callable, Dict
from concurrent.futures import ThreadPoolExecutor

class CronJob:
    def __init__(self, job_id: int, name: str, cron_expr: str, func: Callable, enabled: bool = True):
        self.id = job_id
        self.name = name
        self.cron_expr = cron_expr.strip() if isinstance(cron_expr, str) else ''
        self._minutes, self._hours = self._parse_schedule(self.cron_expr)
        self.func = func
        self.enabled = enabled
        self.last_run_date = ""
        self._job_lock = threading.Lock()
        self.is_running = False
        self._schedule_lock = threading.Lock()
        self._last_run_minute = None

    @staticmethod
    def _parse_schedule(expr):
        """Supported: HH:MM, every_minute, and minute hour * * *."""
        if expr == 'every_minute':
            return set(range(60)), set(range(24))
        daily = re.fullmatch(r'(\d{1,2}):(\d{2})', expr)
        if daily and int(daily[1]) < 24 and int(daily[2]) < 60:
            return {int(daily[2])}, {int(daily[1])}
        fields = expr.split()
        if len(fields) == 5 and fields[2:] == ['*', '*', '*']:
            def values(field, maximum):
                if field == '*':
                    return set(range(maximum + 1))
                if re.fullmatch(r'\d+', field) and int(field) <= maximum:
                    return {int(field)}
                if re.fullmatch(r'\*/\d+', field):
                    interval = int(field[2:])
                    if 1 <= interval <= maximum:
                        return set(range(0, maximum + 1, interval))
                raise ValueError(f'Unsupported cron field: {field!r}')
            return values(fields[0], 59), values(fields[1], 23)
        raise ValueError(f'Unsupported cron expression: {expr!r}; use HH:MM, every_minute, or minute hour * * *')

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
            self.last_run_date = current_time.strftime('%Y-%m-%d')
            return True

class CronManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(CronManager, cls).__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self):
        self.jobs: Dict[int, CronJob] = {}
        self._next_id = 1
        self.running = False
        self._thread = None
        self.executor = None
        self._log_lock = threading.Lock()
        self._log_callback = print  # 可以被替换为往通道发消息的 callback

    def set_log_callback(self, callback: Callable):
        self._log_callback = callback

    def add_job(self, name: str, cron_expr: str, func: Callable, enabled: bool = True) -> int:
        job_id = self._next_id
        job = CronJob(job_id, name, cron_expr, func, enabled)
        self._next_id += 1
        self.jobs[job_id] = job
        return job_id

    def list_jobs(self) -> str:
        if not self.jobs:
            return "当前没有任何定时任务。"
        
        lines = ["📅 **系统定时任务列表**"]
        for job_id, job in self.jobs.items():
            status = "✅ 开启" if job.enabled else "⏸️ 暂停"
            lines.append(f"[{job.id}] {status} | **{job.name}** (时间: {job.cron_expr})")
        lines.append("\n💡 提示: 发送 `/cron <序号>` 手动执行，发送 `/cron toggle <序号>` 开启或暂停任务。")
        return "\n".join(lines)

    def toggle_job(self, job_id: int) -> str:
        if job_id not in self.jobs:
            return f"❌ 找不到序号为 {job_id} 的任务。"
        job = self.jobs[job_id]
        job.enabled = not job.enabled
        state = "已开启" if job.enabled else "已暂停"
        return f"✅ 任务 [{job.id}] {job.name} {state}。"

    def run_job_manually(self, job_id: int) -> str:
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
        if self.running:
            return
        self.running = True
        self.executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="CronWorker")
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="CronEngine")
        self._thread.start()
        print("🕒 定时任务引擎已启动。")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        if self.executor:
            self.executor.shutdown(wait=False)

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
            for job in self.jobs.values():
                if job.should_run(now):
                    try:
                        self.executor.submit(self._execute_job_task, job)
                    except RuntimeError:
                        pass
            
            # 休眠到下一分钟的开始
            time.sleep(60 - datetime.now().second)
