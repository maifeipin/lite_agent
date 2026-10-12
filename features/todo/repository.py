"""Todo repository: SQLite data access layer for Todo items and source receipts.
Strictly lazy: no database or file operations occur upon module import.
All mutations execute within atomic BEGIN IMMEDIATE transactions.
"""

import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data')
DEFAULT_DB_PATH = os.path.join(DATA_DIR, 'todos.db')

_schema_lock = threading.RLock()
_db_initialized = False
_initialized_path: Optional[str] = None
_initialized_file_id: Optional[Tuple[int, int]] = None


def resolve_db_path(db_path: Optional[str] = None) -> str:
    """Resolve database path, defaulting to project data/todos.db."""
    if db_path:
        return os.path.abspath(os.path.expanduser(db_path))
    return os.path.abspath(DEFAULT_DB_PATH)


def _ensure_schema(db_path: Optional[str] = None) -> None:
    """Lazy schema initialization protected by thread lock and process file lock."""
    target = resolve_db_path(db_path)
    with _schema_lock:
        global _db_initialized, _initialized_path, _initialized_file_id
        if _db_initialized and _initialized_path == target:
            try:
                stat = os.stat(target)
                if _initialized_file_id == (stat.st_dev, stat.st_ino):
                    return
            except FileNotFoundError:
                pass

        os.makedirs(os.path.dirname(target), exist_ok=True)
        lock_path = target + '.schema.lock'
        with open(lock_path, 'a') as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                _ensure_schema_locked(target)
            finally:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
                except Exception:
                    pass


def _ensure_schema_locked(target: str) -> None:
    global _db_initialized, _initialized_path, _initialized_file_id
    os.makedirs(os.path.dirname(target), exist_ok=True)
    conn = sqlite3.connect(target)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')

        conn.execute("""
            CREATE TABLE IF NOT EXISTS todos (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              description TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              kind TEXT NOT NULL DEFAULT 'misc',
              project TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              started_at TEXT,
              snoozed_until TEXT,
              shelved_at TEXT,
              shelved_reason TEXT,
              completed_at TEXT,
              dropped_at TEXT,
              dropped_reason TEXT,
              due_at TEXT,
              notified_due_at TEXT,
              recur_cron TEXT,
              recur_parent_id TEXT,
              recur_last_spawned_at TEXT,
              created_via TEXT,
              source_message_id TEXT,
              dispatch_target TEXT,
              dispatch_payload_json TEXT,
              dispatch_last_run_at TEXT,
              FOREIGN KEY (recur_parent_id) REFERENCES todos(id) ON DELETE CASCADE
            )
        """)
        existing = {r[1] for r in conn.execute('PRAGMA table_info(todos)')}
        for name, declaration in (
            ('remind_interval_mins', 'INTEGER DEFAULT 0'),
            ('remind_before_mins', 'INTEGER DEFAULT 30'),
            ('source_key', 'TEXT'),
            ('source_metadata_json', "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if name not in existing:
                conn.execute(f'ALTER TABLE todos ADD COLUMN {name} {declaration}')

        conn.execute("""CREATE TABLE IF NOT EXISTS todo_source_receipts (
            source_key TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            cycle_id INTEGER NOT NULL,
            event_type TEXT NOT NULL
        )""")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status, updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_project ON todos(project, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_wake ON todos(snoozed_until) WHERE status='snoozed'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_due ON todos(due_at) WHERE due_at IS NOT NULL AND notified_due_at IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_recur ON todos(kind, recur_last_spawned_at) WHERE kind='recurring'")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_todos_source_key ON todos(source_key) WHERE source_key IS NOT NULL")

        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()

    _db_initialized = True
    _initialized_path = target
    try:
        stat = os.stat(target)
        _initialized_file_id = (stat.st_dev, stat.st_ino)
    except FileNotFoundError:
        _initialized_file_id = None


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Return an active connection with foreign keys enabled, ensuring schema lazily."""
    target = resolve_db_path(db_path)
    _ensure_schema(target)
    conn = sqlite3.connect(target)
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def generate_todo_id(title: str, db_path: Optional[str] = None) -> str:
    """Generate a unique 6-character hex hash ID with collision checks (up to 5 attempts)."""
    base = hashlib.sha256(f"{title}-{time.time_ns()}".encode()).hexdigest()[:6]
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        for attempt in range(5):
            candidate = base if attempt == 0 else hashlib.sha256(f"{base}-{attempt}".encode()).hexdigest()[:6]
            cursor.execute("SELECT 1 FROM todos WHERE id=?", (candidate,))
            if not cursor.fetchone():
                return candidate
        raise RuntimeError("ID collision after 5 attempts")
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_version(metadata: Dict[str, Any]) -> Tuple[Optional[int], int]:
    revision = metadata.get('revision')
    cycle = metadata.get('cycle_id', 1)
    if revision is not None:
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in (revision, cycle)):
            raise ValueError('revision and cycle_id must be positive integers')
    return revision, cycle


def _source_is_stale(conn: sqlite3.Connection, source_key: str, meta: Dict[str, Any], revision: Optional[int], cycle: int) -> bool:
    receipt = conn.execute(
        'SELECT revision, cycle_id, event_type FROM todo_source_receipts WHERE source_key=?',
        (source_key,)
    ).fetchone()
    current_rev = max(meta.get('revision', 0), receipt[0] if receipt else 0)
    current_cycle = max(meta.get('cycle_id', 1), receipt[1] if receipt else 1)
    return revision is not None and (revision <= current_rev or cycle < current_cycle)


def _save_source_receipt(conn: sqlite3.Connection, source_key: str, revision: Optional[int], cycle: int, event_type: str) -> None:
    if revision is not None:
        conn.execute("""INSERT INTO todo_source_receipts VALUES(?,?,?,?)
            ON CONFLICT(source_key) DO UPDATE SET revision=excluded.revision,
            cycle_id=excluded.cycle_id, event_type=excluded.event_type""",
            (source_key, revision, cycle, event_type))


class TodoRepository:
    """Encapsulates pure database operations for Todo items."""

    def __init__(self, db_path: Optional[str] = None):
        self._custom_db_path = db_path

    @property
    def db_path(self) -> str:
        return resolve_db_path(self._custom_db_path)

    def connect(self) -> sqlite3.Connection:
        return get_connection(self.db_path)

    def _ensure_schema(self) -> None:
        _ensure_schema(self.db_path)

    def _get_connection(self) -> sqlite3.Connection:
        return self.connect()

    def get_by_id(self, tid: str) -> Optional[Dict[str, Any]]:
        return self.get_todo(tid)

    @contextmanager
    def transaction(self):
        """Context manager providing an atomic BEGIN IMMEDIATE transaction connection."""
        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upsert_source_receipt_atomic(
        self,
        source_key: str,
        title: str,
        description: Optional[str] = None,
        created_via: str = 'mail_quality',
        source_message_id: Optional[str] = None,
        source_metadata: Optional[Dict[str, Any]] = None,
        due_at: Optional[str] = None,
        kind: str = 'code',
        project: Optional[str] = 'mail-statement-parser'
    ) -> Dict[str, Any]:
        """Source-key uniqueness and monotone receipts enforced under one atomic transaction."""
        if not source_key or not source_key.strip():
            raise ValueError('source_key 不能为空')
        source_key = source_key.strip()
        incoming = dict(source_metadata or {})
        revision, cycle = _source_version(incoming)
        incoming.update(machine_title=title, machine_description=description, machine_due_at=due_at)

        conn = self.connect()
        now = _now_iso()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("""
                SELECT id, status, title, description, due_at, source_metadata_json, created_via
                FROM todos WHERE source_key=?
            """, (source_key,)).fetchone()

            meta = json.loads(row[5] or '{}') if row else {}
            stale = _source_is_stale(conn, source_key, meta, revision, cycle)
            if stale:
                reason = 'already_done_same_cycle' if row and row[1] in ('done', 'completed') and cycle <= meta.get('cycle_id', 1) else 'stale_event'
                conn.commit()
                return {'id': row[0] if row else None, 'created': False, 'status': 'protected', 'reason': reason}

            _save_source_receipt(conn, source_key, revision, cycle, incoming.get('event_type', 'quality_detected'))

            if row is None:
                tid = uuid.uuid4().hex[:12]
                conn.execute("""
                    INSERT INTO todos(id, title, description, status, kind, project, created_at,
                        updated_at, due_at, created_via, source_message_id, source_key, source_metadata_json)
                    VALUES(?,?,?,'pending',?,?,?,?,?,?,?,?,?)
                """, (tid, title, description, kind, project, now, now, due_at, created_via,
                      source_message_id, source_key, json.dumps(incoming, ensure_ascii=False)))
                result = {'id': tid, 'created': True, 'status': 'created'}
            else:
                tid, status, old_title, old_desc, old_due, _, owner = row
                old_cycle = meta.get('cycle_id', 1)
                human = bool(meta.get('user_edited')) or owner != created_via
                protected = status == 'dropped' or (status in ('done', 'completed') and (cycle <= old_cycle or human))

                incoming.pop('user_edited', None)
                meta.update(incoming)
                conn.execute('UPDATE todos SET source_metadata_json=?, updated_at=? WHERE id=?',
                             (json.dumps(meta, ensure_ascii=False), now, tid))

                if protected:
                    result = {
                        'id': tid,
                        'created': False,
                        'status': 'protected',
                        'reason': 'dropped' if status == 'dropped' else 'already_done_same_cycle'
                    }
                else:
                    reopen = status in ('done', 'completed') and cycle > old_cycle
                    if status not in ('snoozed', 'shelved') and not human:
                        conn.execute("""
                            UPDATE todos SET title=?, description=?, due_at=? WHERE id=?
                        """, (title, description if description is not None else old_desc,
                              old_due if old_due is not None else due_at, tid))
                    if reopen:
                        conn.execute("UPDATE todos SET status='pending', completed_at=NULL WHERE id=?", (tid,))
                    result = {'id': tid, 'created': False, 'status': 'updated'}
                    if reopen:
                        result['reason'] = 'relapse_reopened'

            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def resolve_source_receipt_atomic(
        self,
        source_key: str,
        resolution_note: str = '上游问题已解决',
        cycle_id: Optional[int] = None,
        revision: Optional[int] = None
    ) -> Dict[str, Any]:
        """Atomically persist versioned resolution, tombstone, and status update."""
        if not source_key or not source_key.strip():
            raise ValueError('source_key 不能为空')
        source_key = source_key.strip()
        revision, cycle = _source_version({'revision': revision, 'cycle_id': cycle_id or 1})

        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                'SELECT id, status, source_metadata_json, created_via FROM todos WHERE source_key=?',
                (source_key,)
            ).fetchone()
            meta = json.loads(row[2] or '{}') if row else {}
            if _source_is_stale(conn, source_key, meta, revision, cycle):
                conn.commit()
                return {'id': row[0] if row else None, 'resolved': False, 'reason': 'stale_event', 'status': 'protected'}

            _save_source_receipt(conn, source_key, revision, cycle, 'quality_resolved')
            if row is None:
                conn.commit()
                return {'id': None, 'resolved': False, 'reason': 'resolution_recorded', 'status': 'protected'}

            tid, status, _, created_via = row
            if cycle < meta.get('cycle_id', 1):
                conn.commit()
                return {'id': tid, 'resolved': False, 'reason': 'stale_cycle', 'status': 'protected'}

            meta.update({
                'resolution_note': resolution_note,
                'resolved_cycle_id': cycle,
                'resolved_at': _now_iso(),
                'event_type': 'quality_resolved'
            })
            if revision is not None:
                meta.update(revision=revision, cycle_id=cycle)

            close = status in ('pending', 'active') and created_via == 'mail_quality' and not meta.get('user_edited')
            conn.execute('UPDATE todos SET source_metadata_json=?, updated_at=? WHERE id=?',
                         (json.dumps(meta, ensure_ascii=False), _now_iso(), tid))
            if close:
                conn.execute("UPDATE todos SET status='done', completed_at=? WHERE id=?", (_now_iso(), tid))
            conn.commit()
            return {'id': tid, 'resolved': close, 'status': 'done' if close else 'protected'}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_todo_status_atomic(self, tid: str, status: str, extra_fields: Optional[Dict[str, Any]] = None) -> int:
        """Atomically update status and flag human edits when active."""
        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            now = _now_iso()
            fields = {"status": status, "updated_at": now}
            if extra_fields:
                fields.update(extra_fields)

            if status == 'active':
                row = conn.execute('SELECT source_key, source_metadata_json FROM todos WHERE id=?', (tid,)).fetchone()
                if row and row[0]:
                    metadata = json.loads(row[1] or '{}')
                    metadata['user_edited'] = True
                    fields['source_metadata_json'] = json.dumps(metadata, ensure_ascii=False)

            set_clause = ", ".join([f"{k}=?" for k in fields.keys()])
            values = list(fields.values()) + [tid]
            cursor = conn.cursor()
            cursor.execute(f"UPDATE todos SET {set_clause} WHERE id=?", values)
            rowcount = cursor.rowcount
            conn.commit()
            return rowcount
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_todo_atomic(
        self,
        tid: str,
        title: str,
        description: Optional[str] = None,
        kind: str = "misc",
        project: Optional[str] = None,
        due_at: Optional[str] = None,
        recur_cron: Optional[str] = None,
        remind_interval_mins: int = 0,
        remind_before_mins: int = 30
    ) -> str:
        """Atomically insert a new manual Todo or recurring template."""
        conn = self.connect()
        now = _now_iso()
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("""
                INSERT INTO todos (id, title, description, status, kind, project, created_at, updated_at,
                                   due_at, recur_cron, remind_interval_mins, remind_before_mins)
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (tid, title, description, kind, project, now, now, due_at, recur_cron,
                  remind_interval_mins, remind_before_mins))
            conn.commit()
            return tid
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_todo_fields_atomic(self, tid: str, fields: Dict[str, Any]) -> int:
        """Atomically update arbitrary fields, marking user_edited if modified."""
        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            fields["updated_at"] = _now_iso()
            cursor = conn.cursor()

            if len(fields) > 1:
                cursor.execute("SELECT source_metadata_json FROM todos WHERE id=?", (tid,))
                row = cursor.fetchone()
                if row and row[0]:
                    try:
                        m = json.loads(row[0])
                    except Exception:
                        m = {}
                    m["user_edited"] = True
                    fields["source_metadata_json"] = json.dumps(m, ensure_ascii=False)

            set_clause = ", ".join([f"{k}=?" for k in fields.keys()])
            values = list(fields.values()) + [tid]
            cursor.execute(f"UPDATE todos SET {set_clause} WHERE id=?", values)
            rowcount = cursor.rowcount
            conn.commit()
            return rowcount
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def delete_todo_atomic(self, tid: str) -> int:
        """Atomically delete a Todo by ID."""
        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            cursor = conn.cursor()
            cursor.execute("DELETE FROM todos WHERE id=?", (tid,))
            rowcount = cursor.rowcount
            conn.commit()
            return rowcount
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_todo(self, tid: str) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM todos WHERE id=?", (tid,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_todos(
        self,
        status: Optional[str] = "pending,active",
        project: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        conn = self.connect()
        try:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM todos WHERE 1=1"
            params: List[Any] = []
            if status and status != 'all':
                statuses = [s.strip() for s in status.split(',') if s.strip()]
                if statuses:
                    query += f" AND status IN ({','.join(['?'] * len(statuses))})"
                    params.extend(statuses)
            if project:
                query += " AND project = ?"
                params.append(project)
            if kind:
                query += " AND kind = ?"
                params.append(kind)
            query += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_active_todos_for_rendering(self) -> List[Dict[str, Any]]:
        """Fetch all non-dropped todos for Markdown generation."""
        conn = self.connect()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM todos WHERE status != 'dropped' ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_todos_for_brief(self) -> List[Dict[str, Any]]:
        """专用于每日待办简报的读模型：
        完整获取未完成待办 (pending, active, snoozed)，
        严格按照基线排序：优先有到期日、到期日 ASC (最早逾期最前)、updated_at DESC。
        绝不静默截断。
        """
        self._ensure_schema()
        conn = self.connect()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, title, status, kind, project, due_at, description, created_at, updated_at
                FROM todos
                WHERE status IN ('pending', 'active', 'snoozed')
                ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at ASC, updated_at DESC
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def process_tick_wake_snoozed(self, now: str) -> int:
        """唤醒已到期的 snoozed 待办。"""
        self._ensure_schema()
        with self.transaction() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE todos SET status='pending', snoozed_until=NULL, updated_at=?
                WHERE status='snoozed' AND datetime(snoozed_until) <= datetime(?)
            """, (now, now))
            return cur.rowcount

    def process_tick_due_soon(self, now: str) -> List[Dict[str, Any]]:
        """获取需要提醒的 24h 内到期或已超期待办并原子标记 notified_due_at。"""
        self._ensure_schema()
        with self.transaction() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, title, due_at, created_via, project, kind, description
                FROM todos
                WHERE status IN ('pending', 'active')
                  AND due_at IS NOT NULL
                  AND notified_due_at IS NULL
                  AND datetime(due_at) <= datetime(?, '+24 hours')
                ORDER BY due_at ASC
            """, (now,))
            rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
            for r in rows:
                conn.execute("UPDATE todos SET notified_due_at=?, updated_at=? WHERE id=?", (now, now, r['id']))
            return rows

    def get_recurring_templates(self) -> List[Dict[str, Any]]:
        """获取未废弃的循环待办模板。"""
        self._ensure_schema()
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, title, description, project, recur_cron, recur_last_spawned_at
                FROM todos
                WHERE kind = 'recurring' AND status != 'dropped'
            """)
            return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
        finally:
            conn.close()

    def spawn_recurring_child(self, child_id: str, title: str, description: Optional[str],
                              project: Optional[str], now: str, parent_id: str, due_time: str,
                              expected_last: Optional[str] = None) -> bool:
        """原子派生循环待办子任务并更新模板的 recur_last_spawned_at。"""
        self._ensure_schema()
        with self.transaction() as conn:
            changed = conn.execute("""UPDATE todos SET recur_last_spawned_at=?, updated_at=?
                WHERE id=? AND kind='recurring' AND status!='dropped'
                AND recur_last_spawned_at IS ?""", (now, now, parent_id, expected_last))
            if changed.rowcount != 1:
                return False
            conn.execute("""
                INSERT INTO todos (id, title, description, status, kind, project, created_at, updated_at, recur_parent_id, due_at)
                VALUES (?, ?, ?, 'pending', 'todo', ?, ?, ?, ?, ?)
            """, (child_id, title, description, project, now, now, parent_id, due_time))
            return True

    def get_due_reminder_candidates(self) -> List[Dict[str, Any]]:
        """获取所有待办中需要考虑提醒的候选列表。"""
        self._ensure_schema()
        conn = self.connect()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, title, due_at, created_via, project, kind, description,
                       remind_interval_mins, remind_before_mins, notified_due_at
                FROM todos
                WHERE status IN ('pending', 'active') AND due_at IS NOT NULL
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_notified_due(self, tid: str, now: str, expected_last: Optional[str] = None,
                          expected_due: Optional[str] = None) -> bool:
        """Claim a reminder only if the candidate is still current and actionable."""
        with self.transaction() as conn:
            result = conn.execute("""UPDATE todos SET notified_due_at=?, updated_at=?
                WHERE id=? AND status IN ('pending','active')
                AND notified_due_at IS ? AND due_at IS ?""",
                (now, now, tid, expected_last, expected_due))
            return result.rowcount == 1
