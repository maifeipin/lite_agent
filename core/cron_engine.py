import time
import threading
import traceback
from datetime import datetime
from typing import Callable, Dict, List

class CronJob:
    def __init__(self, job_id: int, name: str, cron_expr: str, func: Callable, enabled: bool = True):
        self.id = job_id
        self.name = name
        self.cron_expr = cron_expr  # 暂定为每天的小时:分钟，如 "02:00"，或者 "every_minute" 等
        self.func = func
        self.enabled = enabled
        self.last_run_date = ""

    def should_run(self, current_time: datetime) -> bool:
        if not self.enabled:
            return False
            
        today_str = current_time.strftime("%Y-%m-%d")
        
        if ":" in self.cron_expr:
            # 支持每N分钟格式: */N * * * *
            if self.cron_expr.startswith("*/") and self.cron_expr.endswith(" * * * *"):
                try:
                    interval = int(self.cron_expr[2:self.cron_expr.index(" ")])
                    minute = current_time.minute
                    if minute % interval == 0:
                        last_minute_key = f"{self.name}_last_minute"
                        last_min = getattr(self, last_minute_key, None)
                        if last_min != minute:
                            setattr(self, last_minute_key, minute)
                            return True
                except (ValueError, IndexError):
                    pass
                return False
            current_hm = current_time.strftime("%H:%M")
            if current_hm == self.cron_expr and self.last_run_date != today_str:
                self.last_run_date = today_str
                return True
        elif self.cron_expr == "every_minute":
            return True
            
        return False

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
        self._log_callback = print  # 可以被替换为往通道发消息的 callback

    def set_log_callback(self, callback: Callable):
        self._log_callback = callback

    def add_job(self, name: str, cron_expr: str, func: Callable, enabled: bool = True) -> int:
        job_id = self._next_id
        self._next_id += 1
        job = CronJob(job_id, name, cron_expr, func, enabled)
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
            traceback.print_exc()
            return f"❌ 手动执行 [{job.name}] 失败: {e}"

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="CronEngine")
        self._thread.start()
        print("🕒 定时任务引擎已启动。")

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _run_loop(self):
        while self.running:
            now = datetime.now()
            for job in self.jobs.values():
                if job.should_run(now):
                    try:
                        self._log_callback(f"🕒 正在自动执行定时任务: {job.name}...")
                        res = job.func()
                        if res:
                            self._log_callback(f"✅ 定时任务 [{job.name}] 执行完毕:\n{res}")
                    except Exception as e:
                        traceback.print_exc()
                        self._log_callback(f"❌ 定时任务 [{job.name}] 执行失败: {e}")
            
            # 休眠到下一分钟的开始
            time.sleep(60 - datetime.now().second)
