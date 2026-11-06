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
        self.max_workers = 5
        # 存活观测：调度循环心跳 (monotonic) 与在途作业 (自动与手动统一登记)
        self._last_tick: Optional[float] = None
        self._inflight: Dict[str, Dict[str, Any]] = {}
        self._inflight_lock = threading.Lock()
        self._inflight_cond = threading.Condition(self._inflight_lock)
        # 关闭中：拒绝新的手动/自动执行，便于 stop 排空既有在途任务
        self._stopping = False
        self._stop_in_progress = False
        self._stop_lock = threading.Lock()
        self._generation = 0

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

    def _mark_inflight(self, name: str, via_pool: bool):
        """登记在途作业。自动 (线程池) 与手动 (调用线程) 统一登记, 供 stop 排空。"""
        with self._inflight_lock:
            self._inflight[name] = {"started": time.monotonic(), "via_pool": via_pool}

    def _clear_inflight(self, name: str):
        with self._inflight_lock:
            self._inflight.pop(name, None)
            self._inflight_cond.notify_all()

    def _wait_inflight(self, timeout: Optional[float]):
        """等待所有在途作业结束 (自动线程池 + 手动调用线程)。timeout=None 表示无限等待。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._inflight_cond:
            while self._inflight:
                if deadline is None:
                    self._inflight_cond.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    self._inflight_cond.wait(remaining)

    def run_job_manually(self, job_id: int) -> str:
        # 与 stop 的关闭标志在同一临界区判定并登记: 关闭开始后不再接收新执行,
        # 已接收的手动执行会被 stop 排空后才释放插件资源。
        with self._lock:
            if self._stopping:
                return "⏳ 调度器正在关闭，已拒绝新的手动执行。"
            if job_id not in self.jobs:
                return f"❌ 找不到序号为 {job_id} 的任务。"
            job = self.jobs[job_id]
            # 与调度循环 (_execute_job_task) 共用同一执行锁: 在途时拒绝并发, 不绕过
            with job._job_lock:
                if job.is_running:
                    return f"⏳ 任务 [{job.name}] 正在执行中，请稍后再试。"
                job.is_running = True
            self._mark_inflight(job.name, via_pool=False)
        try:
            result = job.func()
            return f"🚀 手动执行 [{job.name}] 成功:\n{result}"
        except Exception as e:
            err_msg = traceback.format_exc()
            return f"❌ 手动执行 [{job.name}] 失败: {e}\n{err_msg}"
        finally:
            self._clear_inflight(job.name)
            with job._job_lock:
                job.is_running = False

    def start(self):
        with self._lock:
            if self._stop_in_progress:
                raise RuntimeError("调度器正在停止并排空任务，请等待 stop 完成后再启动。")
            if self.running:
                return
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("上一次调度循环尚未退出，不能启动新的调度循环。")
            with self._inflight_lock:
                if self._stopping and self._inflight:
                    raise RuntimeError("上一次执行尚未排空，不能重新启动调度器。")
            self._stopping = False  # 允许 stop 后重新启动, 恢复接收新执行
            self._generation += 1
            self.running = True
            self.executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="CronWorker")
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="CronEngine")
            self._thread.start()

    def stop(self, wait: bool = True, timeout: Optional[float] = 3.0):
        """停止调度并排空在途任务。

        - 先置关闭标志: 拒绝新的手动/自动执行;
        - 停止调度循环并关闭线程池;
        - wait=True 时等待自动线程池与手动调用线程上的全部在途任务结束
          (timeout=None 表示无限等待, 用于应用关闭确保插件资源不被提前释放)。
        """
        # 序列化停止调用；只处理本次捕获的循环和执行器，不能误关后续实例。
        with self._stop_lock:
            with self._lock:
                self._stop_in_progress = True
                self._stopping = True
                self.running = False
                thread, executor = self._thread, self.executor
            try:
                if thread is not None and thread.is_alive():
                    thread.join(timeout=timeout)
                if executor is not None:
                    try:
                        executor.shutdown(wait=wait, cancel_futures=True)
                    except TypeError:
                        executor.shutdown(wait=wait)
                if wait:
                    self._wait_inflight(timeout)
            finally:
                with self._lock:
                    if self.executor is executor:
                        self.executor = None
                    self._stop_in_progress = False

    def _execute_job_task(self, job: CronJob, generation: Optional[int] = None):
        with self._lock:
            if self._stopping or (generation is not None and generation != self._generation):
                return
            with job._job_lock:
                if job.is_running:
                    return
                job.is_running = True
            self._mark_inflight(job.name, via_pool=True)
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
            self._clear_inflight(job.name)
            with job._job_lock:
                job.is_running = False

    def health_snapshot(self) -> Dict[str, Any]:
        """返回调度器存活快照，供 /health 与外部监控判断是否卡死。"""
        now = time.monotonic()
        with self._inflight_lock:
            inflight = {name: now - rec["started"] for name, rec in self._inflight.items()}
            pool_busy = sum(1 for rec in self._inflight.values() if rec["via_pool"])
        longest = max(inflight.items(), key=lambda kv: kv[1]) if inflight else None
        return {
            "running": self.running,
            "loop_age_s": None if self._last_tick is None else round(now - self._last_tick, 1),
            "busy_workers": pool_busy,
            "max_workers": self.max_workers,
            "longest_job": None if longest is None else {"name": longest[0], "age_s": round(longest[1], 1)},
        }

    def _run_loop(self):
        while self.running:
            self._last_tick = time.monotonic()
            now = datetime.now()
            with self._lock:
                if not self.running:
                    break
                jobs_to_run = [job for job in self.jobs.values() if job.should_run(now)]
                executor, generation = self.executor, self._generation
            for job in jobs_to_run:
                try:
                    if executor:
                        executor.submit(self._execute_job_task, job, generation)
                except RuntimeError:
                    pass
            # 睡眠 1 秒检测停止，避免长 sleep 导致无法退出
            for _ in range(60 - datetime.now().second):
                if not self.running:
                    break
                self._last_tick = time.monotonic()
                time.sleep(1)


default_cron_service = CronService()
