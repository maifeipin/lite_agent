"""
PC CLI SQLite 持久化仓储 (features.pc_cli.repository).
管理 pc_nodes、cli_bots、cli_turns、cli_events（带单调递增 seq_id 供断线续传）与 cli_approvals。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

from lite_agent.features.pc_cli.contracts import (
    ApprovalDecision,
    ApprovalType,
    BotStatus,
    NodeStatus,
    TurnStatus,
    now_ms,
)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pc_nodes (
    node_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'unknown',
    engines_json TEXT NOT NULL DEFAULT '[]',
    engine_models_json TEXT NOT NULL DEFAULT '{}',
    workspace_roots_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'offline',
    last_seen_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cli_bots (
    bot_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    node_id TEXT NOT NULL,
    engine TEXT NOT NULL,
    cwd TEXT NOT NULL,
    approval_mode TEXT NOT NULL DEFAULT 'auto',
    execution_mode TEXT NOT NULL DEFAULT 'agent',
    reasoning_effort TEXT NOT NULL DEFAULT '',
    model TEXT,
    native_session_id TEXT,
    status TEXT NOT NULL DEFAULT 'idle',
    last_summary TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cli_turns (
    turn_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    prompt TEXT NOT NULL,
    input_source TEXT NOT NULL DEFAULT 'text',
    status TEXT NOT NULL DEFAULT 'queued',
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_cli_turns_bot_status ON cli_turns(bot_id, status);

CREATE TABLE IF NOT EXISTS cli_events (
    seq_id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cli_events_bot_seq ON cli_events(bot_id, seq_id);

CREATE TABLE IF NOT EXISTS cli_approvals (
    approval_id TEXT PRIMARY KEY,
    approval_type TEXT NOT NULL DEFAULT 'cli_tool',
    requested_by TEXT NOT NULL DEFAULT 'daemon',
    bot_id TEXT NOT NULL,
    turn_id TEXT,
    tool_name TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    raw_request_json TEXT NOT NULL DEFAULT '{}',
    decision TEXT NOT NULL DEFAULT 'pending',
    input_source TEXT NOT NULL DEFAULT 'button',
    created_at INTEGER NOT NULL,
    decided_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_cli_approvals_pending ON cli_approvals(decision, bot_id);

CREATE TABLE IF NOT EXISTS cli_manager_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cli_manager_usage_created ON cli_manager_usage(created_at);
"""


class PcCliRepository:
    """线程安全的 SQLite 仓储封装。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        if db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.executescript(_SCHEMA_SQL)
            self._ensure_column("pc_nodes", "engine_models_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("cli_bots", "execution_mode", "TEXT NOT NULL DEFAULT 'agent'")
            self._ensure_column("cli_bots", "reasoning_effort", "TEXT NOT NULL DEFAULT ''")
            # 兼容旧库迁移：先将同一 Bot 多余的历史活跃回合收敛为 failed，再建部分唯一索引
            self._conn.execute(
                """
                UPDATE cli_turns
                SET status = 'failed',
                    finished_at = COALESCE(finished_at, ?),
                    error_message = COALESCE(error_message, 'migration_dedup_stale_active_turn')
                WHERE status IN ('queued', 'running')
                  AND rowid NOT IN (
                      SELECT MAX(rowid) FROM cli_turns
                      WHERE status IN ('queued', 'running')
                      GROUP BY bot_id
                  )
                """,
                (now_ms(),),
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_cli_turns_one_active_per_bot
                ON cli_turns(bot_id) WHERE status IN ('queued', 'running')
                """
            )
            self._conn.commit()

    def _ensure_column(self, table: str, column: str, col_def: str) -> None:
        cols = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # PC Nodes
    # ------------------------------------------------------------------
    def upsert_node(
        self,
        node_id: str,
        display_name: str,
        platform: str,
        engines: List[str],
        workspace_roots: List[str],
        status: str = NodeStatus.ONLINE.value,
        engine_models: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ts = now_ms()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pc_nodes (
                    node_id, display_name, platform, engines_json, engine_models_json,
                    workspace_roots_json, status, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    platform = excluded.platform,
                    engines_json = excluded.engines_json,
                    engine_models_json = excluded.engine_models_json,
                    workspace_roots_json = excluded.workspace_roots_json,
                    status = excluded.status,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    node_id,
                    display_name or node_id,
                    platform or "unknown",
                    json.dumps(engines, ensure_ascii=False),
                    json.dumps(engine_models or {}, ensure_ascii=False),
                    json.dumps(workspace_roots, ensure_ascii=False),
                    status,
                    ts,
                ),
            )
            self._conn.commit()
        return self.get_node(node_id) or {}

    def touch_node_heartbeat(self, node_id: str) -> None:
        ts = now_ms()
        with self._lock:
            self._conn.execute(
                "UPDATE pc_nodes SET status = ?, last_seen_at = ? WHERE node_id = ?",
                (NodeStatus.ONLINE.value, ts, node_id),
            )
            self._conn.commit()

    def mark_node_offline(self, node_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE pc_nodes SET status = ? WHERE node_id = ?",
                (NodeStatus.OFFLINE.value, node_id),
            )
            self._conn.commit()

    def mark_all_nodes_offline(self) -> None:
        with self._lock:
            self._conn.execute("UPDATE pc_nodes SET status = ?", (NodeStatus.OFFLINE.value,))
            self._conn.commit()

    def _row_to_node(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "node_id": row["node_id"],
            "display_name": row["display_name"],
            "platform": row["platform"],
            "engines": json.loads(row["engines_json"] or "[]"),
            "engine_models": json.loads(row["engine_models_json"] or "{}"),
            "workspace_roots": json.loads(row["workspace_roots_json"] or "[]"),
            "status": row["status"],
            "last_seen_at": row["last_seen_at"],
        }

    def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM pc_nodes WHERE node_id = ?", (node_id,)).fetchone()
            return self._row_to_node(row) if row else None

    def list_nodes(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pc_nodes ORDER BY CASE WHEN status = 'online' THEN 0 ELSE 1 END, node_id ASC"
            ).fetchall()
            return [self._row_to_node(r) for r in rows]

    # ------------------------------------------------------------------
    # CLI Bots
    # ------------------------------------------------------------------
    def create_bot(
        self,
        *,
        name: str,
        node_id: str,
        engine: str,
        cwd: str,
        approval_mode: str = "auto",
        execution_mode: str = "agent",
        reasoning_effort: str = "",
        model: Optional[str] = None,
        bot_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        bid = bot_id or f"bot_{uuid.uuid4().hex[:12]}"
        ts = now_ms()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cli_bots (
                    bot_id, name, node_id, engine, cwd, approval_mode, execution_mode, reasoning_effort, model,
                    native_session_id, status, last_summary, archived, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, '', 0, ?, ?)
                """,
                (
                    bid, name, node_id, engine, cwd, approval_mode,
                    execution_mode or "agent", reasoning_effort or "", model,
                    BotStatus.IDLE.value, ts, ts,
                ),
            )
            self._conn.commit()
        return self.get_bot(bid) or {}

    def _row_to_bot(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "bot_id": row["bot_id"],
            "name": row["name"],
            "node_id": row["node_id"],
            "engine": row["engine"],
            "cwd": row["cwd"],
            "approval_mode": row["approval_mode"],
            "execution_mode": row["execution_mode"] or "agent",
            "reasoning_effort": row["reasoning_effort"] or "",
            "model": row["model"],
            "native_session_id": row["native_session_id"],
            "status": row["status"],
            "last_summary": row["last_summary"],
            "archived": bool(row["archived"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_bot(self, bot_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM cli_bots WHERE bot_id = ?", (bot_id,)).fetchone()
            return self._row_to_bot(row) if row else None

    def list_bots(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            sql = """
                SELECT b.*,
                       COALESCE(n.status, 'offline') AS node_status,
                       COALESCE(n.display_name, b.node_id) AS node_display_name,
                       (SELECT COALESCE(MAX(e.seq_id), 0) FROM cli_events e WHERE e.bot_id = b.bot_id) AS last_seq_id
                FROM cli_bots b
                LEFT JOIN pc_nodes n ON b.node_id = n.node_id
            """
            if not include_archived:
                sql += " WHERE b.archived = 0"
            sql += " ORDER BY b.updated_at DESC"
            rows = self._conn.execute(sql).fetchall()
            result = []
            for r in rows:
                item = self._row_to_bot(r)
                item["node_status"] = r["node_status"]
                item["node_display_name"] = r["node_display_name"]
                item["last_seq_id"] = r["last_seq_id"]
                result.append(item)
            return result

    def update_bot(
        self,
        bot_id: str,
        *,
        name: Optional[str] = None,
        approval_mode: Optional[str] = None,
        execution_mode: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        model: Optional[str] = None,
        archived: Optional[bool] = None,
        reset_session: bool = False,
    ) -> Optional[Dict[str, Any]]:
        fields: List[str] = []
        values: List[Any] = []
        if name is not None:
            fields.append("name = ?")
            values.append(name)
        if approval_mode is not None:
            fields.append("approval_mode = ?")
            values.append(approval_mode)
        if execution_mode is not None:
            fields.append("execution_mode = ?")
            values.append(execution_mode)
        if reasoning_effort is not None:
            fields.append("reasoning_effort = ?")
            values.append(reasoning_effort)
        if model is not None:
            fields.append("model = ?")
            values.append(model or None)
        if archived is not None:
            fields.append("archived = ?")
            values.append(1 if archived else 0)
        if reset_session:
            fields.append("native_session_id = NULL")
        if not fields:
            return self.get_bot(bot_id)
        fields.append("updated_at = ?")
        values.append(now_ms())
        values.append(bot_id)
        with self._lock:
            self._conn.execute(f"UPDATE cli_bots SET {', '.join(fields)} WHERE bot_id = ?", values)
            self._conn.commit()
        return self.get_bot(bot_id)

    def set_bot_status(
        self,
        bot_id: str,
        status: str,
        last_summary: Optional[str] = None,
        native_session_id: Optional[str] = None,
    ) -> None:
        fields = ["status = ?", "updated_at = ?"]
        values: List[Any] = [status, now_ms()]
        if last_summary is not None:
            fields.append("last_summary = ?")
            values.append(last_summary[:300])
        if native_session_id:
            fields.append("native_session_id = ?")
            values.append(native_session_id)
        values.append(bot_id)
        with self._lock:
            self._conn.execute(f"UPDATE cli_bots SET {', '.join(fields)} WHERE bot_id = ?", values)
            self._conn.commit()

    def delete_bot(self, bot_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM cli_bots WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM cli_events WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM cli_turns WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM cli_approvals WHERE bot_id = ?", (bot_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # CLI Turns
    # ------------------------------------------------------------------
    def create_turn(
        self,
        *,
        bot_id: str,
        prompt: str,
        input_source: str = "text",
        turn_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        tid = turn_id or f"turn_{uuid.uuid4().hex[:12]}"
        ts = now_ms()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cli_turns (turn_id, bot_id, prompt, input_source, status, started_at, finished_at, error_message)
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (tid, bot_id, prompt, input_source, TurnStatus.RUNNING.value, ts),
            )
            self._conn.commit()
        return self.get_turn(tid) or {}

    def start_turn_atomic(
        self,
        *,
        bot_id: str,
        prompt: str,
        input_source: str = "text",
        summary: str = "",
        turn_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """单事务原子写入 cli_turns + 首条 user_message 事件 + 更新 cli_bots 状态，避免部分提交。"""
        tid = turn_id or f"turn_{uuid.uuid4().hex[:12]}"
        ts = now_ms()
        payload = {"text": prompt, "input_source": input_source}
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    """
                    INSERT INTO cli_turns (turn_id, bot_id, prompt, input_source, status, started_at, finished_at, error_message)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (tid, bot_id, prompt, input_source, TurnStatus.RUNNING.value, ts),
                )
                cur = self._conn.execute(
                    """
                    INSERT INTO cli_events (bot_id, turn_id, event_type, payload_json, created_at)
                    VALUES (?, ?, 'user_message', ?, ?)
                    """,
                    (bot_id, tid, payload_json, ts),
                )
                seq_id = int(cur.lastrowid)
                self._conn.execute(
                    "UPDATE cli_bots SET status = ?, last_summary = ?, updated_at = ? WHERE bot_id = ?",
                    (BotStatus.RUNNING.value, summary[:300], ts, bot_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        turn = self.get_turn(tid) or {}
        event = {
            "seq_id": seq_id,
            "bot_id": bot_id,
            "turn_id": tid,
            "event_type": "user_message",
            "payload": payload,
            "created_at": ts,
        }
        return turn, event

    def get_turn(self, turn_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM cli_turns WHERE turn_id = ?", (turn_id,)).fetchone()
            return dict(row) if row else None

    def get_active_turn_for_bot(self, bot_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cli_turns WHERE bot_id = ? AND status IN ('queued', 'running') ORDER BY started_at DESC LIMIT 1",
                (bot_id,),
            ).fetchone()
            return dict(row) if row else None

    def finish_turn(
        self,
        turn_id: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        ts = now_ms()
        with self._lock:
            self._conn.execute(
                "UPDATE cli_turns SET status = ?, finished_at = ?, error_message = ? WHERE turn_id = ?",
                (status, ts, error_message, turn_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # CLI Events (seq_id monotonic stream)
    # ------------------------------------------------------------------
    def append_event(
        self,
        *,
        bot_id: str,
        turn_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        ts = now_ms()
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO cli_events (bot_id, turn_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (bot_id, turn_id, event_type, payload_json, ts),
            )
            seq_id = int(cur.lastrowid)
            self._conn.commit()
        return {
            "seq_id": seq_id,
            "bot_id": bot_id,
            "turn_id": turn_id,
            "event_type": event_type,
            "payload": payload,
            "created_at": ts,
        }

    def list_events(
        self,
        bot_id: str,
        after_seq: int = 0,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT seq_id, bot_id, turn_id, event_type, payload_json, created_at
                FROM cli_events
                WHERE bot_id = ? AND seq_id > ?
                ORDER BY seq_id ASC
                LIMIT ?
                """,
                (bot_id, int(after_seq), safe_limit),
            ).fetchall()
            out = []
            for r in rows:
                out.append({
                    "seq_id": r["seq_id"],
                    "bot_id": r["bot_id"],
                    "turn_id": r["turn_id"],
                    "event_type": r["event_type"],
                    "payload": json.loads(r["payload_json"] or "{}"),
                    "created_at": r["created_at"],
                })
            return out

    def list_recent_events(self, bot_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """按 seq_id 倒序取最新 N 条事件，再按时间正序返回（不受 1,000 条旧日志截断影响）。"""
        safe_limit = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT seq_id, bot_id, turn_id, event_type, payload_json, created_at
                FROM cli_events
                WHERE bot_id = ?
                ORDER BY seq_id DESC
                LIMIT ?
                """,
                (bot_id, safe_limit),
            ).fetchall()
        out = [
            {
                "seq_id": r["seq_id"],
                "bot_id": r["bot_id"],
                "turn_id": r["turn_id"],
                "event_type": r["event_type"],
                "payload": json.loads(r["payload_json"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in reversed(rows)
        ]
        return out

    # ------------------------------------------------------------------
    # CLI Approvals (Atomic CAS)
    # ------------------------------------------------------------------
    def create_approval(
        self,
        *,
        bot_id: str,
        tool_name: str,
        summary: str,
        raw_request: Optional[Dict[str, Any]] = None,
        turn_id: Optional[str] = None,
        approval_type: str = ApprovalType.CLI_TOOL.value,
        requested_by: str = "daemon",
        approval_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        aid = approval_id or f"appr_{uuid.uuid4().hex[:12]}"
        ts = now_ms()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cli_approvals (
                    approval_id, approval_type, requested_by, bot_id, turn_id,
                    tool_name, summary, raw_request_json, decision, input_source, created_at, decided_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'button', ?, NULL)
                """,
                (
                    aid,
                    approval_type,
                    requested_by,
                    bot_id,
                    turn_id,
                    tool_name,
                    summary,
                    json.dumps(raw_request or {}, ensure_ascii=False),
                    ApprovalDecision.PENDING.value,
                    ts,
                ),
            )
            self._conn.commit()
        return self.get_approval(aid) or {}

    def _row_to_approval(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "approval_id": row["approval_id"],
            "approval_type": row["approval_type"],
            "requested_by": row["requested_by"],
            "bot_id": row["bot_id"],
            "turn_id": row["turn_id"],
            "tool_name": row["tool_name"],
            "summary": row["summary"],
            "raw_request": json.loads(row["raw_request_json"] or "{}"),
            "decision": row["decision"],
            "input_source": row["input_source"],
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
        }

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM cli_approvals WHERE approval_id = ?", (approval_id,)).fetchone()
            return self._row_to_approval(row) if row else None

    def list_pending_approvals(self, bot_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if bot_id:
                rows = self._conn.execute(
                    "SELECT * FROM cli_approvals WHERE decision = 'pending' AND bot_id = ? ORDER BY created_at ASC",
                    (bot_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM cli_approvals WHERE decision = 'pending' ORDER BY created_at ASC"
                ).fetchall()
            return [self._row_to_approval(r) for r in rows]

    def resolve_approval_cas(
        self,
        approval_id: str,
        decision: str,
        input_source: str = "button",
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """原子 CAS 更新审批单，防止重复消费。"""
        ts = now_ms()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE cli_approvals
                SET decision = ?, input_source = ?, decided_at = ?
                WHERE approval_id = ? AND decision = 'pending'
                """,
                (decision, input_source, ts, approval_id),
            )
            self._conn.commit()
            updated = cur.rowcount == 1
        return updated, self.get_approval(approval_id)

    def mark_approval_expired(self, approval_id: str) -> Optional[Dict[str, Any]]:
        """将未能送达或已失效的审批单标记为 expired。"""
        ts = now_ms()
        with self._lock:
            self._conn.execute(
                "UPDATE cli_approvals SET decision = ?, decided_at = ? WHERE approval_id = ?",
                (ApprovalDecision.EXPIRED.value, ts, approval_id),
            )
            self._conn.commit()
        return self.get_approval(approval_id)

    def expire_pending_approvals(
        self,
        *,
        bot_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> int:
        """批量失效指定回合、Bot 或离线节点下的 pending cli_tool 审批单。"""
        ts = now_ms()
        with self._lock:
            if turn_id:
                cur = self._conn.execute(
                    """
                    UPDATE cli_approvals
                    SET decision = ?, decided_at = ?
                    WHERE decision = 'pending' AND approval_type IN ('cli_tool', 'turn_cancel') AND turn_id = ?
                    """,
                    (ApprovalDecision.EXPIRED.value, ts, turn_id),
                )
            elif bot_id:
                cur = self._conn.execute(
                    """
                    UPDATE cli_approvals
                    SET decision = ?, decided_at = ?
                    WHERE decision = 'pending' AND approval_type = 'cli_tool' AND bot_id = ?
                    """,
                    (ApprovalDecision.EXPIRED.value, ts, bot_id),
                )
            elif node_id:
                cur = self._conn.execute(
                    """
                    UPDATE cli_approvals
                    SET decision = ?, decided_at = ?
                    WHERE decision = 'pending' AND approval_type = 'cli_tool'
                      AND bot_id IN (SELECT bot_id FROM cli_bots WHERE node_id = ?)
                    """,
                    (ApprovalDecision.EXPIRED.value, ts, node_id),
                )
            else:
                return 0
            self._conn.commit()
            return int(cur.rowcount)

    # ------------------------------------------------------------------
    # Manager Usage & Dashboard Aggregation
    # ------------------------------------------------------------------
    def record_manager_usage(self, *, model_name: str, tokens_used: int = 0, tool_calls: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cli_manager_usage (model_name, tokens_used, tool_calls, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (model_name or "unknown", max(0, int(tokens_used or 0)), max(0, int(tool_calls or 0)), now_ms()),
            )
            self._conn.commit()

    def get_dashboard_stats(self, days: int = 7) -> Dict[str, Any]:
        safe_days = max(1, min(int(days or 7), 90))
        since_ms = now_ms() - safe_days * 86_400_000
        with self._lock:
            node_rows = self._conn.execute("SELECT status, COUNT(*) AS cnt FROM pc_nodes GROUP BY status").fetchall()
            node_counts = {r["status"]: int(r["cnt"]) for r in node_rows}

            bot_rows = self._conn.execute(
                "SELECT status, archived, COUNT(*) AS cnt FROM cli_bots GROUP BY status, archived"
            ).fetchall()
            bot_stats = {"total": 0, "active": 0, "archived": 0, "idle": 0, "running": 0, "waiting_approval": 0, "error": 0}
            for r in bot_rows:
                cnt = int(r["cnt"])
                bot_stats["total"] += cnt
                if r["archived"]:
                    bot_stats["archived"] += cnt
                else:
                    bot_stats["active"] += cnt
                    st = r["status"]
                    if st in bot_stats:
                        bot_stats[st] += cnt

            turn_rows = self._conn.execute(
                """
                SELECT t.status, t.started_at, t.finished_at, b.engine, b.node_id
                FROM cli_turns t
                LEFT JOIN cli_bots b ON t.bot_id = b.bot_id
                WHERE t.started_at >= ?
                """,
                (since_ms,),
            ).fetchall()

            t_total = len(turn_rows)
            t_completed = t_failed = t_cancelled = t_running = 0
            durations: List[int] = []
            by_engine: Dict[str, int] = {}
            by_node: Dict[str, int] = {}
            for r in turn_rows:
                st = r["status"]
                if st == "completed":
                    t_completed += 1
                elif st == "failed":
                    t_failed += 1
                elif st == "cancelled":
                    t_cancelled += 1
                else:
                    t_running += 1
                if r["finished_at"] and r["started_at"] and r["finished_at"] >= r["started_at"]:
                    durations.append(int(r["finished_at"] - r["started_at"]))
                eng = r["engine"] or "unknown"
                by_engine[eng] = by_engine.get(eng, 0) + 1
                nid = r["node_id"] or "unknown"
                by_node[nid] = by_node.get(nid, 0) + 1

            finished_total = t_completed + t_failed + t_cancelled
            success_rate = round((t_completed / finished_total) * 100, 1) if finished_total > 0 else 0.0
            avg_duration_ms = int(sum(durations) / len(durations)) if durations else 0

            mgr_row = self._conn.execute(
                """
                SELECT COUNT(*) AS calls, COALESCE(SUM(tokens_used), 0) AS total_tokens
                FROM cli_manager_usage
                WHERE created_at >= ?
                """,
                (since_ms,),
            ).fetchone()

            pending_approvals = self.list_pending_approvals()

        return {
            "days": safe_days,
            "nodes": {
                "total": sum(node_counts.values()),
                "online": node_counts.get("online", 0),
                "offline": node_counts.get("offline", 0),
            },
            "bots": bot_stats,
            "turns": {
                "total": t_total,
                "completed": t_completed,
                "failed": t_failed,
                "cancelled": t_cancelled,
                "running": t_running,
                "success_rate": success_rate,
                "avg_duration_ms": avg_duration_ms,
                "by_engine": by_engine,
                "by_node": by_node,
            },
            "manager_usage": {
                "calls": int(mgr_row["calls"]) if mgr_row else 0,
                "total_tokens": int(mgr_row["total_tokens"]) if mgr_row else 0,
            },
            "pending_approvals_count": len(pending_approvals),
            "pending_approvals": pending_approvals,
        }
