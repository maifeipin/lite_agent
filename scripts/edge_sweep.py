#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Edge Sentinel 任务超时回收 (cron 调用)。

扫 edge_tasks 表, dispatched 超 task_timeout_min 未 ack 的任务回 pending
(nonce 不变, 防绕过去重)。由 vps1 cron 每分钟调用。

独立脚本而非 command 内联 python, 避免 config command 的 .format(root=)
与 f-string 的 {var} 冲突 (KeyError)。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import edge_db

if __name__ == "__main__":
    n = edge_db.sweep_timeouts(edge_db._DEFAULT_TIMEOUT_MIN)
    if n:
        print(f"edge_sweep: recovered {n} timed-out task(s) back to pending")
