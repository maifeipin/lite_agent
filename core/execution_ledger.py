"""
Phase 3a — 轻量 SQLite 执行账本 (旁路记录器)

核心原则：
  - Runtime 负责产生事实事件
  - Ledger 负责持久化事实
  - Agent/Worker 只负责连接两者

不将数据库连接放进 AgentRuntime，也不让 Runtime 理解 SQLite 表结构。
所有写入都是"尽力记录"，失败不阻断 Agent 主流程。

两张表 (Phase 3a 最小可交付):
  executions      — 一次完整 Runtime 执行的概要
  runtime_events  — Runtime 事件序列 (事实的主要来源)

状态机:
  running → terminal (succeeded / failed / dead_loop / max_steps /
                       token_budget_exceeded / abandoned)
  终态不可再覆盖 (单向变化)，避免后续 DONE 把更具体的失败状态覆盖为成功。
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

from core.constants import PROJECT_ROOT
from core.execution import ExecutionContext

logger = logging.getLogger(__name__)


# ============================================================
#  常量
# ============================================================

DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "workspace", "execution_ledger.db")

# Schema 版本；后续迁移时递增
SCHEMA_VERSION = 2

# 终态集合：一旦进入，不可再变更
TERMINAL_STATUSES = frozenset({
    "succeeded", "failed", "dead_loop", "max_steps",
    "token_budget_exceeded", "abandoned",
})

# 事件类型 → execution 终态映射
EVENT_TO_TERMINAL = {
    "ERROR": "failed",
    "DEAD_LOOP": "dead_loop",
    "MAX_STEPS": "max_steps",
    "TOKEN_BUDGET_EXCEEDED": "token_budget_exceeded",
    "DONE": "succeeded",
}

# 崩溃恢复阈值：超过此时间仍 running 的记录视为崩溃遗留 (秒)
ABANDON_THRESHOLD_SECONDS = 3600  # 1 小时


# ============================================================
#  Schema
# ============================================================

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id        TEXT PRIMARY KEY,
    parent_execution_id TEXT,
    session_key         TEXT,
    actor_id            TEXT NOT NULL,
    actor_type          TEXT NOT NULL,
    source              TEXT NOT NULL,

    model_name          TEXT,
    provider            TEXT,
    stream_mode         INTEGER NOT NULL DEFAULT 0,

    status              TEXT NOT NULL,
    terminal_reason     TEXT,

    max_steps           INTEGER,
    max_output_tokens   INTEGER,
    token_budget        INTEGER,

    steps_used          INTEGER NOT NULL DEFAULT 0,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,

    started_at          REAL NOT NULL,
    finished_at         REAL,
    duration_ms         INTEGER,

    created_at          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_executions_session ON executions(session_key);
CREATE INDEX IF NOT EXISTS idx_executions_status  ON executions(status);
CREATE INDEX IF NOT EXISTS idx_executions_parent  ON executions(parent_execution_id);
CREATE INDEX IF NOT EXISTS idx_executions_started ON executions(started_at);

CREATE TABLE IF NOT EXISTS runtime_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id    TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    event_type      TEXT NOT NULL,

    step            INTEGER,
    tool_call_id    TEXT,
    tool_name       TEXT,

    payload_json    TEXT NOT NULL,
    created_at      REAL NOT NULL,

    UNIQUE(execution_id, seq),
    FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
);

CREATE INDEX IF NOT EXISTS idx_events_exec_seq  ON runtime_events(execution_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_type     ON runtime_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_tool      ON runtime_events(tool_name);

CREATE TABLE IF NOT EXISTS tool_invocations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_call_id    TEXT NOT NULL,
    execution_id    TEXT NOT NULL,
    session_key     TEXT,
    step            INTEGER,
    tool_name       TEXT NOT NULL,

    -- 调用侧 (TOOL_CALL 投影)
    arguments_hash  TEXT,
    arguments_length INTEGER,

    -- 结果侧 (TOOL_RESULT 投影, 可能未完成则为空)
    ok              INTEGER,
    output_hash     TEXT,
    output_length   INTEGER,
    duration_ms     INTEGER,

    called_at       REAL NOT NULL,
    returned_at     REAL,

    UNIQUE(execution_id, tool_call_id),
    FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_inv_exec    ON tool_invocations(execution_id);
CREATE INDEX IF NOT EXISTS idx_tool_inv_name    ON tool_invocations(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_inv_session ON tool_invocations(session_key);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ============================================================
#  执行句柄
# ============================================================

@dataclass
class ExecutionHandle:
    """Ledger.start() 返回的执行句柄。
    Agent 持有此句柄并传给 recorder.wrap()。
    """
    id: str
    parent_execution_id: str = ""
    started_at: float = field(default_factory=time.time)


# ============================================================
#  ExecutionLedger
# ============================================================

class ExecutionLedger:
    """SQLite 执行账本。

    线程安全：使用进程级锁 + WAL 模式。
    失败策略：所有写入 try/except，不阻断主流程，仅记录到 logger。
    """

    # 进程级写锁，避免 SQLite "database is locked"
    _write_lock = threading.Lock()

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._ensure_dir()
        self._init_schema()
        self._migrate_schema()

    # ---- 公开 API ----

    def new_execution_id(self) -> str:
        """生成新的 execution_id (UUID4)。
        使用 UUID 而非 SQLite 自增 ID，便于跨进程导入备份。
        """
        return uuid.uuid4().hex

    def start(self, ctx: ExecutionContext, model_name: str = "",
              provider: str = "", parent_execution_id: str = "",
              stream_mode: bool = False) -> ExecutionHandle:
        """开始一次执行记录，返回句柄。

        若 ctx.execution_id 已存在则复用，否则分配新 ID。
        """
        execution_id = ctx.execution_id or self.new_execution_id()
        started_at = time.time()
        token_budget = ctx.token_budget if ctx.token_budget is not None else None

        # 解析后的 parent_execution_id 与数据库一致，保证句柄与表一致
        resolved_parent = parent_execution_id or ctx.parent_execution_id

        row = {
            "execution_id": execution_id,
            "parent_execution_id": resolved_parent,
            "session_key": ctx.session_key,
            "actor_id": ctx.actor_id,
            "actor_type": ctx.actor_type.value,
            "source": ctx.source.value,
            "model_name": model_name,
            "provider": provider,
            "stream_mode": 1 if stream_mode else 0,
            "status": "running",
            "terminal_reason": None,
            "max_steps": ctx.max_steps,
            "max_output_tokens": ctx.max_output_tokens,
            "token_budget": token_budget,
            "steps_used": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "started_at": started_at,
            "finished_at": None,
            "duration_ms": None,
            "created_at": started_at,
        }

        self._safe_write(
            "INSERT OR IGNORE INTO executions "
            "(execution_id, parent_execution_id, session_key, actor_id, actor_type, source, "
            " model_name, provider, stream_mode, status, terminal_reason, "
            " max_steps, max_output_tokens, token_budget, steps_used, "
            " prompt_tokens, completion_tokens, total_tokens, "
            " started_at, finished_at, duration_ms, created_at) "
            "VALUES (:execution_id, :parent_execution_id, :session_key, :actor_id, :actor_type, :source, "
            " :model_name, :provider, :stream_mode, :status, :terminal_reason, "
            " :max_steps, :max_output_tokens, :token_budget, :steps_used, "
            " :prompt_tokens, :completion_tokens, :total_tokens, "
            " :started_at, :finished_at, :duration_ms, :created_at)",
            row,
        )
        return ExecutionHandle(id=execution_id,
                               parent_execution_id=resolved_parent,
                               started_at=started_at)

    def record_event(self, execution_id: str, seq: int, event_type: str,
                     payload: dict, step: Optional[int] = None,
                     tool_call_id: Optional[str] = None,
                     tool_name: Optional[str] = None) -> None:
        """记录一条 Runtime 事件 (INSERT OR IGNORE 防重复)。

        seq 保证事件顺序可还原；同一 (execution_id, seq) 重复写入不会产生重复行。

        注意: 此方法只写事件表，不更新概要。请优先使用 record_and_project()
        以保证事件去重与概要投影的原子性。
        """
        row = {
            "execution_id": execution_id,
            "seq": seq,
            "event_type": event_type,
            "step": step,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "payload_json": json.dumps(payload, ensure_ascii=False, default=str),
            "created_at": time.time(),
        }
        self._safe_write(
            "INSERT OR IGNORE INTO runtime_events "
            "(execution_id, seq, event_type, step, tool_call_id, tool_name, "
            " payload_json, created_at) "
            "VALUES (:execution_id, :seq, :event_type, :step, :tool_call_id, :tool_name, "
            " :payload_json, :created_at)",
            row,
        )

    def record_and_project(self, execution_id: str, seq: int, event_type: str,
                          payload: dict, step: Optional[int] = None,
                          tool_call_id: Optional[str] = None,
                          tool_name: Optional[str] = None) -> bool:
        """原子地记录事件并投影概要。

        在同一事务中：
          1. INSERT OR IGNORE runtime_events (按 (execution_id, seq) 去重)
          2. 若 INSERT 真正生效 (rowcount=1)，才更新 executions 概要

        返回 True 表示事件被新增 (非重复)；False 表示重复被忽略，概要未更新。
        这保证了重放的 USAGE/STEP_START 不会重复累加 token / 步数。
        """
        row = {
            "execution_id": execution_id,
            "seq": seq,
            "event_type": event_type,
            "step": step,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "payload_json": json.dumps(payload, ensure_ascii=False, default=str),
            "created_at": time.time(),
        }
        try:
            with self._write_lock:
                with self._connect() as conn:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO runtime_events "
                        "(execution_id, seq, event_type, step, tool_call_id, tool_name, "
                        " payload_json, created_at) "
                        "VALUES (:execution_id, :seq, :event_type, :step, :tool_call_id, :tool_name, "
                        " :payload_json, :created_at)",
                        row,
                    )
                    inserted = cur.rowcount > 0
                    if inserted:
                        self._project_in_txn(conn, execution_id,
                                            event_type, payload,
                                            step=step, tool_call_id=tool_call_id,
                                            tool_name=tool_name)
                    conn.commit()
                    return inserted
        except Exception:
            logger.exception("ExecutionLedger.record_and_project 失败")
            return False

    def record_batch(self, execution_id: str, events: list) -> int:
        """批量原子记录事件并投影概要 (单事务)。

        events: [{seq, event_type, payload, step, tool_call_id, tool_name}, ...]

        在同一事务中：
          1. 对每个事件执行 INSERT OR IGNORE
          2. 若 INSERT 生效，执行 _project_in_txn
          3. 一次性提交

        返回真正新增的事件数 (非重复)。
        用于 TEXT/REASONING delta 聚合后批量写入，降低 SQLite 写放大。
        """
        if not events:
            return 0
        now = time.time()
        try:
            with self._write_lock:
                with self._connect() as conn:
                    inserted_count = 0
                    for ev in events:
                        row = {
                            "execution_id": execution_id,
                            "seq": ev["seq"],
                            "event_type": ev["event_type"],
                            "step": ev.get("step"),
                            "tool_call_id": ev.get("tool_call_id"),
                            "tool_name": ev.get("tool_name"),
                            "payload_json": json.dumps(
                                ev["payload"], ensure_ascii=False, default=str),
                            "created_at": now,
                        }
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO runtime_events "
                            "(execution_id, seq, event_type, step, tool_call_id, tool_name, "
                            " payload_json, created_at) "
                            "VALUES (:execution_id, :seq, :event_type, :step, :tool_call_id, :tool_name, "
                            " :payload_json, :created_at)",
                            row,
                        )
                        if cur.rowcount > 0:
                            inserted_count += 1
                            self._project_in_txn(
                                conn, execution_id,
                                ev["event_type"], ev["payload"],
                                step=ev.get("step"),
                                tool_call_id=ev.get("tool_call_id"),
                                tool_name=ev.get("tool_name"),
                            )
                    conn.commit()
                    return inserted_count
        except Exception:
            logger.exception("ExecutionLedger.record_batch 失败")
            return 0

    def update_execution_on_event(self, execution_id: str, event_type: str,
                                  payload: dict) -> None:
        """根据事件类型更新 executions 表 (steps/token/status)。

        状态只能单向变化: running → terminal。
        终态事件后到达的 DONE 不会覆盖更具体的失败状态。

        注意: 此方法不检查事件是否重复，请优先使用 record_and_project()
        以保证去重与投影的原子性。
        """
        try:
            with self._write_lock:
                with self._connect() as conn:
                    self._project_in_txn(conn, execution_id, event_type, payload)
                    conn.commit()
        except Exception:
            logger.exception("ExecutionLedger.update_execution_on_event 失败")

    def _project_in_txn(self, conn, execution_id: str, event_type: str,
                        payload: dict, step: Optional[int] = None,
                        tool_call_id: Optional[str] = None,
                        tool_name: Optional[str] = None) -> None:
        """在已开启的事务中投影概要 (不提交, 不加锁)。

        供 record_and_project / update_execution_on_event / record_batch 共用。
        终态事件检查当前状态，已是终态则跳过 (单向状态机)。

        tool_invocations 索引投影:
          - TOOL_CALL: INSERT OR IGNORE 一行 (tool_call_id 唯一)
          - TOOL_RESULT: UPDATE 同行填充结果侧字段
        索引表是 runtime_events 的事务内投影，不成为第二事实源。
        """
        # 终态映射
        terminal_status = EVENT_TO_TERMINAL.get(event_type)
        if terminal_status:
            # 检查当前是否已是终态
            cur = conn.execute(
                "SELECT status FROM executions WHERE execution_id = ?",
                (execution_id,))
            row = cur.fetchone()
            current = row[0] if row else None
            if current in TERMINAL_STATUSES:
                # 已是终态，DONE 不能覆盖更具体的失败
                return
            self._set_terminal_in_txn(conn, execution_id,
                                      terminal_status, payload)
            return

        # 非终态事件：更新 steps / token
        if event_type == "STEP_START":
            step = payload.get("step")
            if step is not None:
                # step 是 1-based，存为已执行步数
                conn.execute(
                    "UPDATE executions SET steps_used = MAX(steps_used, ?) "
                    "WHERE execution_id = ? AND status = 'running'",
                    (step, execution_id),
                )
        elif event_type == "USAGE":
            pt = int(payload.get("prompt_tokens", 0) or 0)
            ct = int(payload.get("completion_tokens", 0) or 0)
            tt = int(payload.get("total_tokens", 0) or 0)
            conn.execute(
                "UPDATE executions SET "
                "  prompt_tokens = prompt_tokens + ?, "
                "  completion_tokens = completion_tokens + ?, "
                "  total_tokens = total_tokens + ? "
                "WHERE execution_id = ? AND status = 'running'",
                (pt, ct, tt, execution_id),
            )

        # ---- tool_invocations 索引投影 (事务内, 非第二事实源) ----
        if event_type == "TOOL_CALL":
            self._project_tool_call(conn, execution_id, payload,
                                    step=step, tool_call_id=tool_call_id,
                                    tool_name=tool_name)
        elif event_type == "TOOL_RESULT":
            self._project_tool_result(conn, execution_id, payload,
                                       tool_call_id=tool_call_id,
                                       tool_name=tool_name)

    def _project_tool_call(self, conn, execution_id: str, payload: dict,
                           step: Optional[int] = None,
                           tool_call_id: Optional[str] = None,
                           tool_name: Optional[str] = None) -> None:
        """TOOL_CALL → tool_invocations UPSERT (补齐调用侧字段，不覆盖结果侧)。

        UPSERT 语义: 若行已存在 (异常顺序下 TOOL_RESULT 先到)，仅回填调用侧
        字段 (arguments_hash/length/step/tool_name/called_at)，不覆盖已写入的
        结果侧字段 (ok/output_*/returned_at)。若行不存在，则插入。
        """
        tc_id = tool_call_id or payload.get("id", "")
        name = tool_name or payload.get("name", "")
        if not tc_id:
            return  # 无 tool_call_id 无法索引
        # 查询 session_key (事务内)
        cur = conn.execute(
            "SELECT session_key FROM executions WHERE execution_id = ?",
            (execution_id,))
        row = cur.fetchone()
        session_key = row[0] if row else None
        # UPSERT: 冲突时仅回填调用侧字段，不覆盖结果侧
        conn.execute(
            "INSERT INTO tool_invocations "
            "(tool_call_id, execution_id, session_key, step, tool_name, "
            " arguments_hash, arguments_length, called_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(execution_id, tool_call_id) DO UPDATE SET "
            "  step = COALESCE(excluded.step, tool_invocations.step), "
            "  tool_name = COALESCE(excluded.tool_name, tool_invocations.tool_name), "
            "  arguments_hash = COALESCE(excluded.arguments_hash, tool_invocations.arguments_hash), "
            "  arguments_length = COALESCE(excluded.arguments_length, tool_invocations.arguments_length), "
            "  called_at = COALESCE(excluded.called_at, tool_invocations.called_at)",
            (tc_id, execution_id, session_key, step, name,
             payload.get("arguments_hash"), payload.get("arguments_length"),
             time.time()),
        )

    def _project_tool_result(self, conn, execution_id: str, payload: dict,
                             tool_call_id: Optional[str] = None,
                             tool_name: Optional[str] = None) -> None:
        """TOOL_RESULT → tool_invocations UPSERT (补齐结果侧字段，不覆盖调用侧)。

        UPSERT 语义: 若行已存在 (正常顺序 TOOL_CALL 先到)，仅回填结果侧字段，
        不覆盖调用侧字段。若行不存在 (异常顺序)，则插入并填充结果侧。
        """
        tc_id = tool_call_id or payload.get("id", "")
        name = tool_name or payload.get("name", "")
        if not tc_id:
            return
        ok = 1 if payload.get("ok") else 0
        # 查询 session_key (事务内)
        sess_cur = conn.execute(
            "SELECT session_key FROM executions WHERE execution_id = ?",
            (execution_id,))
        sess_row = sess_cur.fetchone()
        session_key = sess_row[0] if sess_row else None
        # UPSERT: 冲突时仅回填结果侧字段，不覆盖调用侧
        conn.execute(
            "INSERT INTO tool_invocations "
            "(tool_call_id, execution_id, session_key, step, tool_name, "
            " ok, output_hash, output_length, duration_ms, called_at, returned_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(execution_id, tool_call_id) DO UPDATE SET "
            "  tool_name = COALESCE(excluded.tool_name, tool_invocations.tool_name), "
            "  ok = excluded.ok, "
            "  output_hash = excluded.output_hash, "
            "  output_length = excluded.output_length, "
            "  duration_ms = COALESCE(excluded.duration_ms, tool_invocations.duration_ms), "
            "  returned_at = excluded.returned_at",
            (tc_id, execution_id, session_key, name, ok,
             payload.get("output_hash"), payload.get("output_length"),
             payload.get("duration_ms"), time.time(), time.time()),
        )

    def finish(self, execution_id: str, status: str = "succeeded",
               terminal_reason: str = "") -> None:
        """显式结束一次执行。
        若当前已是终态，调用无效 (不可覆盖)。
        """
        current = self._get_status(execution_id)
        if current in TERMINAL_STATUSES:
            return
        now = time.time()
        started_at = self._get_started_at(execution_id) or now
        duration_ms = int((now - started_at) * 1000)
        self._safe_write(
            "UPDATE executions SET status = ?, terminal_reason = ?, "
            "finished_at = ?, duration_ms = ? "
            "WHERE execution_id = ? AND status = 'running'",
            (status, terminal_reason or None, now, duration_ms, execution_id),
        )

    def recover_abandoned(self, threshold_seconds: int = ABANDON_THRESHOLD_SECONDS) -> int:
        """崩溃恢复：将长时间停留在 running 的记录标记为 abandoned。

        只处理超过 threshold_seconds 的记录，避免误伤其他正在运行的进程。
        返回被标记的记录数。
        """
        cutoff = time.time() - threshold_seconds
        now = time.time()
        try:
            with self._write_lock:
                with self._connect() as conn:
                    # duration_ms 按每行 started_at 计算，而非统一阈值
                    cur = conn.execute(
                        "UPDATE executions SET status = 'abandoned', "
                        "terminal_reason = 'process_interrupted', "
                        "finished_at = ?, "
                        "duration_ms = CAST((? - started_at) * 1000 AS INTEGER) "
                        "WHERE status = 'running' AND started_at < ?",
                        (now, now, cutoff),
                    )
                    n = cur.rowcount
                    conn.commit()
                    if n > 0:
                        logger.warning(
                            "ExecutionLedger: %d 个 running 记录标记为 abandoned", n)
                    return n
        except Exception:
            logger.exception("ExecutionLedger.recover_abandoned 失败")
            return 0

    # ---- 只读查询接口 (Phase 3a 基础版) ----

    def get_execution(self, execution_id: str) -> Optional[dict]:
        """查询一次执行的概要。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM executions WHERE execution_id = ?",
                    (execution_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
        except Exception:
            logger.exception("ExecutionLedger.get_execution 失败")
            return None

    def list_events(self, execution_id: str) -> list[dict]:
        """按 seq 顺序返回某次执行的所有事件。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM runtime_events WHERE execution_id = ? "
                    "ORDER BY seq ASC",
                    (execution_id,))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            logger.exception("ExecutionLedger.list_events 失败")
            return []

    # ============================================================
    #  tool_invocations 索引查询
    # ============================================================

    def list_tool_invocations(self, execution_id: str) -> list[dict]:
        """返回某次执行的所有工具调用 (按 called_at 顺序)。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM tool_invocations WHERE execution_id = ? "
                    "ORDER BY called_at ASC",
                    (execution_id,))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            logger.exception("ExecutionLedger.list_tool_invocations 失败")
            return []

    def get_tool_invocation(self, execution_id: str,
                            tool_call_id: str) -> Optional[dict]:
        """按 (execution_id, tool_call_id) 查询单条工具调用。

        tool_call_id 仅在一次 execution 内可靠，不能跨 execution 假设唯一。
        """
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM tool_invocations "
                    "WHERE execution_id = ? AND tool_call_id = ?",
                    (execution_id, tool_call_id))
                cols = [d[0] for d in cur.description]
                row = cur.fetchone()
                return dict(zip(cols, row)) if row else None
        except Exception:
            logger.exception("ExecutionLedger.get_tool_invocation 失败")
            return None

    def find_tool_invocations_by_name(self, tool_name: str,
                                       limit: int = 100) -> list[dict]:
        """按工具名查询最近 N 条调用 (跨执行)。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM tool_invocations WHERE tool_name = ? "
                    "ORDER BY called_at DESC LIMIT ?",
                    (tool_name, limit))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            logger.exception("ExecutionLedger.find_tool_invocations_by_name 失败")
            return []

    def list_tool_invocations_by_session(self, session_key: str,
                                          limit: int = 200) -> list[dict]:
        """按 session 查询工具调用 (跨执行, 用于审计)。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT * FROM tool_invocations WHERE session_key = ? "
                    "ORDER BY called_at DESC LIMIT ?",
                    (session_key, limit))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            logger.exception(
                "ExecutionLedger.list_tool_invocations_by_session 失败")
            return []

    def list_recent(self, status: Optional[str] = None,
                    limit: int = 50) -> list[dict]:
        """列出最近的执行记录。"""
        try:
            with self._connect() as conn:
                if status:
                    cur = conn.execute(
                        "SELECT * FROM executions WHERE status = ? "
                        "ORDER BY started_at DESC LIMIT ?",
                        (status, limit))
                else:
                    cur = conn.execute(
                        "SELECT * FROM executions "
                        "ORDER BY started_at DESC LIMIT ?",
                        (limit,))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            logger.exception("ExecutionLedger.list_recent 失败")
            return []

    def find_abandoned(self, limit: int = 100) -> list[dict]:
        """查找所有 abandoned 记录。"""
        return self.list_recent(status="abandoned", limit=limit)

    # ---- 内部工具 ----

    def _ensure_dir(self):
        d = os.path.dirname(self.db_path)
        if d:
            os.makedirs(d, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        """创建一个新连接 (短生命周期，避免跨线程共享 cursor)。

        WAL 模式 + busy_timeout + foreign_keys 在每次连接时设置。
        """
        conn = sqlite3.connect(self.db_path, timeout=3.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _write_lock_ctx(self):
        """获取进程级写锁。"""
        self._write_lock.acquire()
        try:
            yield
        finally:
            self._write_lock.release()

    def _init_schema(self):
        try:
            with self._write_lock:
                with self._connect() as conn:
                    conn.executescript(_SCHEMA_SQL)
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_meta (key, value) "
                        "VALUES ('version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                    conn.commit()
        except Exception:
            logger.exception("ExecutionLedger schema 初始化失败")

    # v1 → v2: tool_invocations UNIQUE(tool_call_id) → UNIQUE(execution_id, tool_call_id)
    _V2_NEW_TOOL_INVOCATIONS_SQL = """
        CREATE TABLE tool_invocations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_call_id    TEXT NOT NULL,
            execution_id    TEXT NOT NULL,
            session_key     TEXT,
            step            INTEGER,
            tool_name       TEXT NOT NULL,

            arguments_hash  TEXT,
            arguments_length INTEGER,

            ok              INTEGER,
            output_hash     TEXT,
            output_length   INTEGER,
            duration_ms     INTEGER,

            called_at       REAL NOT NULL,
            returned_at     REAL,

            UNIQUE(execution_id, tool_call_id),
            FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
        );
    """

    def _tool_invocations_has_legacy_unique(self, conn) -> bool:
        """检测 tool_invocations 是否仍持有旧的 UNIQUE(tool_call_id) 约束。

        v1 建表语句将 tool_call_id 声明为 UNIQUE，SQLite 会创建一个
        自动索引 (origin='u')，仅覆盖 tool_call_id 单列。

        PRAGMA index_list 行格式: (seq, name, unique, origin, partial)
        """
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='tool_invocations'")
        if cur.fetchone() is None:
            return False
        # 遍历所有 UNIQUE 索引，检查其覆盖的列
        idx_cur = conn.execute("PRAGMA index_list('tool_invocations')")
        for row in idx_cur.fetchall():
            # row: (seq, name, unique, origin, partial)
            if row[2] != 1:  # 非唯一索引跳过
                continue
            origin = row[3]
            idx_name = row[1]
            cols_cur = conn.execute(f"PRAGMA index_info('{idx_name}')")
            cols = [c[2] for c in cols_cur.fetchall()]
            # 仅覆盖 tool_call_id 单列且 origin='u' (表级 UNIQUE) 即旧约束
            if len(cols) == 1 and cols[0] == "tool_call_id" and origin == "u":
                return True
        return False

    def _tool_invocations_v1_exists(self, conn) -> bool:
        """检测是否存在半迁移遗留的 tool_invocations_v1 表。"""
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='tool_invocations_v1'")
        return cur.fetchone() is not None

    def _migrate_schema(self):
        """v1 → v2 迁移: tool_invocations 复合唯一键。

        幂等: 通过 schema_meta 版本号 + 旧约束存在性 + 半迁移恢复三重判断。
        迁移在显式 BEGIN IMMEDIATE 单事务中完成:
        RENAME → CREATE → INSERT...SELECT → DROP → CREATE INDEX → UPDATE version。
        任意步骤失败立即 ROLLBACK，不污染状态。
        """
        try:
            with self._write_lock:
                with self._connect() as conn:
                    # 读取当前版本
                    cur = conn.execute(
                        "SELECT value FROM schema_meta WHERE key = 'version'")
                    row = cur.fetchone()
                    current_version = int(row[0]) if row else 0

                    if current_version >= 2:
                        # 已是 v2，但可能存在半迁移遗留 (上次迁移中途失败被回滚
                        # 但某些 DDL 在 SQLite 中无法事务回滚)。清理遗留表。
                        if self._tool_invocations_v1_exists(conn):
                            conn.execute("DROP TABLE IF EXISTS tool_invocations_v1")
                            conn.commit()
                            logger.warning(
                                "ExecutionLedger 清理半迁移遗留 tool_invocations_v1")
                        return

                    # 检测半迁移状态: v1 表存在但 tool_invocations 可能为旧表或新表
                    if self._tool_invocations_v1_exists(conn):
                        # 半迁移遗留: 旧表已 RENAME，但新表可能未创建或未完成复制。
                        # 这里的恢复策略:
                        #   - 如果 tool_invocations 表不存在 (CREATE 失败)，
                        #     则把 v1 表 RENAME 回 tool_invocations，重新尝试。
                        #   - 如果 tool_invocations 已是新表 (COPY/INDEX 失败)，
                        #     则尝试补齐 COPY/INDEX，完成后 DROP v1。
                        # 由于迁移在事务内失败应回滚，这种情况主要来自 DDL 不能回滚
                        # (SQLite 中 ALTER/CREATE/DROP 在事务外会立即提交)。
                        # 通过检测新表是否有旧唯一约束判断其状态。
                        if not self._has_table(conn, "tool_invocations"):
                            # 新表未创建，恢复旧表
                            conn.execute(
                                "ALTER TABLE tool_invocations_v1 RENAME TO "
                                "tool_invocations")
                            conn.commit()
                            logger.warning(
                                "ExecutionLedger 半迁移回滚: 恢复 v1 表到原名称")
                        else:
                            # 新表已创建但可能未 COPY。补齐数据迁移。
                            existing_count = conn.execute(
                                "SELECT COUNT(*) FROM tool_invocations").fetchone()[0]
                            v1_count = conn.execute(
                                "SELECT COUNT(*) FROM tool_invocations_v1"
                            ).fetchone()[0]
                            if existing_count == 0 and v1_count > 0:
                                # COPY 未完成，补齐
                                conn.execute(
                                    "INSERT INTO tool_invocations "
                                    "(tool_call_id, execution_id, session_key, step, "
                                    " tool_name, arguments_hash, arguments_length, ok, "
                                    " output_hash, output_length, duration_ms, "
                                    " called_at, returned_at) "
                                    "SELECT tool_call_id, execution_id, session_key, "
                                    "       step, tool_name, arguments_hash, "
                                    "       arguments_length, ok, output_hash, "
                                    "       output_length, duration_ms, called_at, "
                                    "       returned_at FROM tool_invocations_v1")
                            conn.execute("DROP TABLE tool_invocations_v1")
                            # 确保索引存在
                            conn.execute(
                                "CREATE INDEX IF NOT EXISTS idx_tool_inv_exec "
                                "ON tool_invocations(execution_id)")
                            conn.execute(
                                "CREATE INDEX IF NOT EXISTS idx_tool_inv_name "
                                "ON tool_invocations(tool_name)")
                            conn.execute(
                                "CREATE INDEX IF NOT EXISTS idx_tool_inv_session "
                                "ON tool_invocations(session_key)")
                            conn.execute(
                                "UPDATE schema_meta SET value = '2' "
                                "WHERE key = 'version'")
                            conn.commit()
                            logger.info("ExecutionLedger 半迁移恢复完成")
                            return

                    # 检测旧约束是否存在 (新建库无旧约束，无需迁移)
                    if not self._tool_invocations_has_legacy_unique(conn):
                        # 新库或已迁移，仅更新版本号
                        conn.execute(
                            "UPDATE schema_meta SET value = '2' "
                            "WHERE key = 'version'")
                        conn.commit()
                        return

                    # v1 → v2: 显式 BEGIN IMMEDIATE 单事务迁移
                    # 注意: SQLite 的 ALTER/CREATE/DROP 是事务安全的，
                    # 但 Python sqlite3 默认 isolation_level 会隐式提交 DDL，
                    # 因此显式 BEGIN + 关闭 autocommit 行为。
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute(
                            "ALTER TABLE tool_invocations "
                            "RENAME TO tool_invocations_v1")
                        conn.execute(self._V2_NEW_TOOL_INVOCATIONS_SQL)
                        conn.execute(
                            "INSERT INTO tool_invocations "
                            "(tool_call_id, execution_id, session_key, step, "
                            " tool_name, arguments_hash, arguments_length, ok, "
                            " output_hash, output_length, duration_ms, "
                            " called_at, returned_at) "
                            "SELECT tool_call_id, execution_id, session_key, step, "
                            "       tool_name, arguments_hash, arguments_length, ok, "
                            "       output_hash, output_length, duration_ms, "
                            "       called_at, returned_at "
                            "FROM tool_invocations_v1")
                        conn.execute("DROP TABLE tool_invocations_v1")
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_tool_inv_exec "
                            "ON tool_invocations(execution_id)")
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_tool_inv_name "
                            "ON tool_invocations(tool_name)")
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_tool_inv_session "
                            "ON tool_invocations(session_key)")
                        conn.execute(
                            "UPDATE schema_meta SET value = '2' "
                            "WHERE key = 'version'")
                        conn.commit()
                        logger.info("ExecutionLedger v1 → v2 迁移完成")
                    except Exception:
                        conn.execute("ROLLBACK")
                        raise
        except Exception:
            logger.exception("ExecutionLedger schema 迁移失败")

    @staticmethod
    def _has_table(conn, table_name: str) -> bool:
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name=?", (table_name,))
        return cur.fetchone() is not None

    def _safe_write(self, sql: str, params: Any):
        """带锁 + try/except 的写入，失败仅记日志。"""
        try:
            with self._write_lock:
                with self._connect() as conn:
                    conn.execute(sql, params)
                    conn.commit()
        except Exception:
            logger.exception("ExecutionLedger 写入失败: %s", sql[:80])

    def _get_status(self, execution_id: str) -> Optional[str]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT status FROM executions WHERE execution_id = ?",
                    (execution_id,))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception:
            logger.exception("ExecutionLedger._get_status 失败")
            return None

    def _get_started_at(self, execution_id: str) -> Optional[float]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT started_at FROM executions WHERE execution_id = ?",
                    (execution_id,))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception:
            return None

    def _set_terminal(self, execution_id: str, status: str,
                     payload: dict) -> None:
        """将 execution 置为终态 (单向，不可再变更)。"""
        try:
            with self._write_lock:
                with self._connect() as conn:
                    self._set_terminal_in_txn(conn, execution_id, status, payload)
                    conn.commit()
        except Exception:
            logger.exception("ExecutionLedger._set_terminal 失败")

    def _set_terminal_in_txn(self, conn, execution_id: str, status: str,
                             payload: dict) -> None:
        """在已开启的事务中将 execution 置为终态 (不提交, 不加锁)。"""
        now = time.time()
        # 在事务内读取 started_at
        cur = conn.execute(
            "SELECT started_at FROM executions WHERE execution_id = ?",
            (execution_id,))
        row = cur.fetchone()
        started_at = row[0] if row else now
        duration_ms = int((now - started_at) * 1000)
        reason = ""
        if status == "failed":
            msg = payload.get("msg", "") or ""
            reason = msg or "model_error"
        elif status == "dead_loop":
            reason = payload.get("msg") or f"dead_loop: {payload.get('tool_name', '')}"
        elif status == "max_steps":
            reason = "max_steps_exceeded"
        elif status == "token_budget_exceeded":
            reason = f"budget={payload.get('budget')}, used={payload.get('used')}"
        elif status == "succeeded":
            reason = payload.get("finish_reason", "stop")

        conn.execute(
            "UPDATE executions SET status = ?, terminal_reason = ?, "
            "finished_at = ?, duration_ms = ? "
            "WHERE execution_id = ? AND status = 'running'",
            (status, reason, now, duration_ms, execution_id),
        )
