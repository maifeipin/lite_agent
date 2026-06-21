#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Edge Sentinel 阶段二: edge_tasks 任务表 + 状态机 (中枢侧)。

设计见 implementation_plan_phase2.md §1.7/§1.8/§4.A1。
状态机: pending → dispatched → done/failed
  - claim_task: pull 时 pending→dispatched, 用 BEGIN IMMEDIATE 事务防多节点抢同一任务
  - submit_result: 收回传结果 → done/failed
  - sweep_timeouts: (废弃) 改为 TCP-like 的 get_stuck_tasks + update_retransmitted_task
nonce 不可变: 任务创建时固定 (hash(task_id)), 重新分发不换 nonce。
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

from core.constants import PROJECT_ROOT as _PROJECT_ROOT
_DB_PATH = os.path.join(_PROJECT_ROOT, "data", "sentinel", "edge_tasks.db")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=10, isolation_level=None)  # autocommit, 手动 BEGIN
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    conn = _conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS edge_tasks (
                id            TEXT PRIMARY KEY,
                node          TEXT NOT NULL,
                cmd           TEXT NOT NULL,
                ts            TEXT NOT NULL,
                nonce         TEXT NOT NULL,
                sig           TEXT NOT NULL,
                key_tier      TEXT NOT NULL,        -- hot / root
                status        TEXT NOT NULL,        -- pending / dispatched / done / failed
                result        TEXT,                 -- JSON {exit_code, stdout, stderr}
                created_at    TEXT NOT NULL,
                dispatched_at TEXT,
                done_at       TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edge_node_status ON edge_tasks(node, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edge_status_dispatched ON edge_tasks(status, dispatched_at)")
        try:
            conn.execute("ALTER TABLE edge_tasks ADD COLUMN retry_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # already exists
    finally:
        conn.close()


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def create_task(task_id: str, node: str, cmd: str, ts: str, nonce: str, sig: str, key_tier: str) -> dict:
    """插入一条 pending 任务。task_id/nonce/ts/sig 由调用方(skill)生成并签名后传入。"""
    init_db()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO edge_tasks (id,node,cmd,ts,nonce,sig,key_tier,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,'pending',?)",
            (task_id, node, cmd, ts, nonce, sig, key_tier, _now()),
        )
        return get_task(task_id)
    finally:
        conn.close()


def claim_task(node: str) -> dict:
    """边缘 pull: 取该 node 最早的一条 pending, 原子置 dispatched。

    BEGIN IMMEDIATE 抢写锁, 保证多节点/多 cron 不会抢到同一任务。"""
    init_db()
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM edge_tasks WHERE node=? AND status='pending' "
            "ORDER BY created_at ASC LIMIT 1",
            (node,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE edge_tasks SET status='dispatched', dispatched_at=? WHERE id=?",
            (_now(), row["id"]),
        )
        conn.execute("COMMIT")
        return dict(row)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def submit_result(task_id: str, exit_code: int, stdout: str, stderr: str) -> bool:
    """边缘回传结果 → done(exit_code==0) / failed。"""
    init_db()
    status = "done" if exit_code == 0 else "failed"
    result = json.dumps({"exit_code": exit_code, "stdout": stdout, "stderr": stderr}, ensure_ascii=False)
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE edge_tasks SET status=?, result=?, done_at=? "
            "WHERE id=? AND status='dispatched'",
            (status, result, _now(), task_id),
        )
        return cur.rowcount > 0
    finally:
        conn.close()


def get_task(task_id: str) -> dict:
    init_db()
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM edge_tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_pending(node: str = None) -> list:
    init_db()
    conn = _conn()
    try:
        if node:
            rows = conn.execute(
                "SELECT * FROM edge_tasks WHERE status='pending' AND node=? ORDER BY created_at",
                (node,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM edge_tasks WHERE status='pending' ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_stuck_tasks(timeout_sec: int) -> list:
    """找出长时间 dispatched 却未收到 ack 的任务 (TCP 重传探测)"""
    init_db()
    cutoff = (datetime.utcnow() - timedelta(seconds=timeout_sec)).isoformat() + "Z"
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM edge_tasks WHERE status='dispatched' AND dispatched_at < ?",
            (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_retransmitted_task(task_id: str, new_ts: str, new_sig: str, max_retries: int) -> bool:
    """原子化更新重传任务。返回 True 表示重签并回退 pending 成功，返回 False 表示重传超限被判死刑。"""
    init_db()
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM edge_tasks WHERE id=? AND status='dispatched'",
            (task_id,)
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            return False

        row_dict = dict(row)
        current_retry = row_dict.get("retry_count", 0) if row_dict.get("retry_count") is not None else 0
        if current_retry < max_retries:
            conn.execute(
                "UPDATE edge_tasks SET status='pending', dispatched_at=NULL, ts=?, sig=?, retry_count=? WHERE id=?",
                (new_ts, new_sig, current_retry + 1, task_id)
            )
            conn.execute("COMMIT")
            return True
        else:
            result = json.dumps({"exit_code": -1, "stdout": "", "stderr": "节点失联，重传失败 (Lost Connection)"}, ensure_ascii=False)
            conn.execute(
                "UPDATE edge_tasks SET status='failed', result=?, done_at=? WHERE id=?",
                (result, _now(), task_id)
            )
            conn.execute("COMMIT")
            return False
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def task_result_text(task_id: str) -> str:
    """把 done/failed 的结果格式化成给 agent/用户看的文本。"""
    t = get_task(task_id)
    if not t:
        return f"❌ 任务 {task_id} 不存在"
    if t["status"] == "pending":
        return f"⏳ 任务 {task_id} 仍在等待边缘节点拉取 (cron 5min 周期)"
    if t["status"] == "dispatched":
        return f"🔄 任务 {task_id} 已下发到 {t['node']}, 等待执行回传"
    r = json.loads(t["result"]) if t["result"] else {}
    head = f"✅ [{t['node']}] {t['cmd']}" if t["status"] == "done" else f"❌ [{t['node']}] {t['cmd']} (exit={r.get('exit_code')})"
    out = r.get("stdout", "")
    err = r.get("stderr", "")
    body = out
    if err:
        body += f"\n[stderr]\n{err}"
    return f"{head}\n{body}" if body else head
