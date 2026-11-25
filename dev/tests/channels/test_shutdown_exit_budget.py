"""验证停机排空预算 (_wait_for_exit_budget) 在存在阻塞线程池在途任务时可按预算干净退出。"""

from __future__ import annotations

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from lite_agent.cli import _wait_for_exit_budget


def test_wait_for_exit_budget_respects_stuck_threadpool_task():
    """真实子进程验证：ThreadPoolExecutor 中存在永不返回的阻塞任务时，按预算退出且返回码为 0。"""
    script = """
import os, sys, time
from concurrent.futures import ThreadPoolExecutor
from lite_agent.cli import _wait_for_exit_budget

pool = ThreadPoolExecutor(max_workers=1)
pool.submit(time.sleep, 60)
time.sleep(0.05)
pool.shutdown(wait=False, cancel_futures=True)

t0 = time.monotonic()
drained = _wait_for_exit_budget(0.4)
dt = time.monotonic() - t0
assert drained is False, f"expected drained=False, got {drained}"
assert dt < 2.0, f"budget wait took too long: {dt:.2f}s"
sys.stdout.flush()
os._exit(0)
"""
    t0 = time.monotonic()
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15.0)
    elapsed = time.monotonic() - t0
    assert res.returncode == 0, f"stderr={res.stderr}"
    assert elapsed < 15.0


def test_wait_for_exit_budget_returns_true_when_pool_finishes():
    """在途任务在预算内结束时返回 True。"""
    pool = ThreadPoolExecutor(max_workers=1)
    pool.submit(time.sleep, 0.05)
    pool.shutdown(wait=False, cancel_futures=True)
    assert _wait_for_exit_budget(2.0) is True
