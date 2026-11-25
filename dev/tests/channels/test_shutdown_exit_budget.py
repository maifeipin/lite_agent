"""真实子进程回归验证：生产 _graceful_exit 在 Cron 卡死、线程池阻塞或日志管道写满时均按统一预算退出。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from lite_agent.cli import _wait_for_exit_budget


def _utf8_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def test_graceful_exit_bounds_stuck_cron_and_still_runs_lifecycle():
    """真实子进程验证生产 _graceful_exit：Cron 作业卡死 60s 时，按预算停止等待、继续执行 lifecycle 清理并以 0 退出。"""
    script = """
import signal, time
from lite_agent.app.lifecycle import LifecycleManager
from lite_agent.cli import _graceful_exit
from lite_agent.services.cron import CronService

cron = CronService()
cron.start()
def stuck_job():
    time.sleep(60)
cron.add_job("stuck", "every_minute", stuck_job)
cron._mark_inflight("stuck", via_pool=True)
cron.executor.submit(stuck_job)
time.sleep(0.05)

lc = LifecycleManager()
lc.register_resource("marker", lambda: print("LIFECYCLE_CLEANED", flush=True))

class _App:
    def __init__(self):
        self.cron_service = cron
        self.lifecycle = lc
    def shutdown(self, timeout=None):
        self.cron_service.stop(wait=True, timeout=timeout)
        return self.lifecycle.shutdown()

_graceful_exit(_App(), signal.SIGTERM, total_budget_sec=1.2)
"""
    res = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_utf8_env(),
        timeout=15.0,
    )
    assert res.returncode == 0, f"stderr={res.stderr}\nstdout={res.stdout}"
    assert "LIFECYCLE_CLEANED" in res.stdout


def test_graceful_exit_bounds_stuck_threadpool_after_shutdown():
    """真实子进程验证生产 _graceful_exit：app.shutdown() 返回后线程池仍有 60s 在途任务时，经生产兜底以 0 退出。"""
    script = """
import signal, time
from concurrent.futures import ThreadPoolExecutor
from lite_agent.cli import _graceful_exit

pool = ThreadPoolExecutor(max_workers=1)
pool.submit(time.sleep, 60)
time.sleep(0.05)

class _App:
    def shutdown(self, timeout=None):
        pool.shutdown(wait=False, cancel_futures=True)
        print("APP_SHUTDOWN_DONE", flush=True)
        return []

_graceful_exit(_App(), signal.SIGTERM, total_budget_sec=1.0)
"""
    res = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_utf8_env(),
        timeout=15.0,
    )
    assert res.returncode == 0, f"stderr={res.stderr}\nstdout={res.stdout}"
    assert "APP_SHUTDOWN_DONE" in res.stdout


def test_graceful_exit_watchdog_exits_when_stdout_pipe_blocked():
    """真实子进程验证：父进程暂停读取 stdout 导致管道写满阻塞时，watchdog 到期仍按预算直接退出且返回 124。"""
    script = """
import signal, sys
from lite_agent.cli import _graceful_exit

class _BlockedShutdownApp:
    def shutdown(self, timeout=None):
        # 持续写 stdout 直到管道写满阻塞，模拟核心清理阶段被日志管道卡死
        chunk = "X" * 65536 + "\\n"
        for _ in range(100):
            sys.stdout.write(chunk)
            sys.stdout.flush()
        return []

_graceful_exit(_BlockedShutdownApp(), signal.SIGTERM, total_budget_sec=0.5)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_utf8_env(),
    )
    try:
        # 故意不读取 stdout，制造写满管道阻塞，验证 watchdog 在 0.5s 预算到期后直接退出
        rc = proc.wait(timeout=10.0)
        assert rc == 124, f"expected forced-timeout exit code 124, got {rc}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3.0)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


def test_wait_for_exit_budget_returns_true_when_pool_finishes():
    """在途任务在预算内结束时返回 True。"""
    pool = ThreadPoolExecutor(max_workers=1)
    pool.submit(time.sleep, 0.05)
    pool.shutdown(wait=False, cancel_futures=True)
    assert _wait_for_exit_budget(2.0) is True
