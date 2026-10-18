"""
Phone App 任务、审批与事件持久化仓储 (features.phone_app.repository).
采用标准 SQLite 数据库与 WAL 模式，确保多进程安全、掉电安全与重启可恢复。
严格遵循第 6 节要求，杜绝纯内存字典。
"""

from __future__ import annotations
import sqlite3
import json
import os
import time
from typing import Dict, Any, List, Optional, Tuple
from lite_agent.features.phone_app.contracts import (
    BatchStatus,
    TaskStatus,
    BATCH_TRANSITIONS,
    TASK_TRANSITIONS,
)


class PhoneAppRepository:
    """持久化仓储。"""

    def __init__(self, db_path: Optional[str] = None):
        if not db_path:
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../data"))
            os.makedirs(base_dir, exist_ok=True)
            db_path = os.path.join(base_dir, "phone_app.db")
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def close(self):
        """显式清理/关闭钩子。"""
        # WAL checkpoint
        try:
            conn = self._get_connection()
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            finally:
                conn.close()
        except Exception:
            pass

    def _init_db(self):
        conn = self._get_connection()
        try:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'fixed',
                revision INTEGER NOT NULL DEFAULT 1,
                action TEXT NOT NULL,
                query TEXT NOT NULL,
                device_id TEXT NOT NULL,
                app_ids_json TEXT NOT NULL,
                status TEXT NOT NULL,
                filters_json TEXT,
                budget_json TEXT,
                scripts_json TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                completed_at INTEGER
            );
            """)

            try:
                conn.execute("ALTER TABLE batches ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;")
            except Exception:
                pass

            conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                app_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                adapter_version TEXT,
                receipt_json TEXT,
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                completed_at INTEGER,
                FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
            );
            """)

            conn.execute("""
            CREATE TABLE IF NOT EXISTS approvals (
                batch_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                reviewer_id TEXT NOT NULL,
                canonical_hash TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0,
                approved_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                consumed_at INTEGER,
                payload_json TEXT NOT NULL
            );
            """)

            conn.execute("""
            CREATE TABLE IF NOT EXISTS device_states (
                device_id TEXT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'ready',
                paused_reason TEXT,
                last_attempt_id TEXT,
                updated_at INTEGER NOT NULL
            );
            """)

            conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                task_id TEXT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_batch ON tasks(batch_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_batch ON events(batch_id);")

            # 重启崩溃恢复门禁 (D01): 将上次异常中断残留的 running/cancel_requested 标记为终态，
            # 并将涉及设备置为 paused，防止重启后在未确认停止的情况下恢复派发造成并发冲突
            now = int(time.time())
            stale_batches = conn.execute(
                "SELECT batch_id, device_id, status FROM batches WHERE status IN ('running', 'cancel_requested')"
            ).fetchall()
            for b in stale_batches:
                did = b["device_id"]
                conn.execute("""
                    INSERT INTO device_states (device_id, state, paused_reason, updated_at)
                    VALUES (?, 'paused', '服务重启：检测到上次未确认完成的任务，暂停设备以待人工核实', ?)
                    ON CONFLICT(device_id) DO UPDATE SET
                    state = 'paused',
                    paused_reason = '服务重启：检测到上次未确认完成的任务，暂停设备以待人工核实',
                    updated_at = ?
                """, (did, now, now))
                final_b_st = "cancelled" if b["status"] == "cancel_requested" else "failed"
                conn.execute("""
                    UPDATE batches SET status = ?, updated_at = ?, completed_at = ?
                    WHERE batch_id = ?
                """, (final_b_st, now, now, b["batch_id"]))
                conn.execute("""
                    UPDATE tasks SET status = 'execution_unknown', error = '服务重启，在途任务执行状态未知中断', updated_at = ?, completed_at = ?
                    WHERE batch_id = ? AND status IN ('running', 'cancel_requested')
                """, (now, now, b["batch_id"]))

            conn.commit()
        finally:
            conn.close()

    # --- Device State Operations ---

    def get_device_state(self, device_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT * FROM device_states WHERE device_id = ?", (device_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def set_device_state(
        self,
        device_id: str,
        state: str,
        reason: Optional[str] = None,
        attempt_id: Optional[str] = None
    ) -> None:
        now = int(time.time())
        conn = self._get_connection()
        try:
            conn.execute("""
            INSERT INTO device_states (device_id, state, paused_reason, last_attempt_id, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                state = excluded.state,
                paused_reason = excluded.paused_reason,
                last_attempt_id = coalesce(excluded.last_attempt_id, device_states.last_attempt_id),
                updated_at = excluded.updated_at
            """, (device_id, state, reason, attempt_id, now))
            conn.commit()
        finally:
            conn.close()

    # --- Batch Operations ---

    def create_batch(
        self,
        batch_id: str,
        action: str,
        query: str,
        device_id: str,
        app_ids: List[str],
        status: str,
        mode: str = "fixed",
        filters: Optional[Dict[str, Any]] = None,
        budget: Optional[Dict[str, Any]] = None,
        scripts: Optional[Dict[str, Any]] = None
    ) -> None:
        now = int(time.time())
        conn = self._get_connection()
        try:
            conn.execute("""
            INSERT INTO batches (
                batch_id, mode, revision, action, query, device_id, app_ids_json, status,
                filters_json, budget_json, scripts_json, created_at, updated_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                batch_id, mode, action, query, device_id, json.dumps(app_ids), status,
                json.dumps(filters or {}), json.dumps(budget or {}), json.dumps(scripts or {}),
                now, now
            ))
            conn.commit()
        finally:
            conn.close()

    def get_batch(self, batch_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["revision"] = d.get("revision", 1)
            d["app_ids"] = json.loads(d.pop("app_ids_json"))
            d["filters"] = json.loads(d.pop("filters_json") or "{}")
            d["budget"] = json.loads(d.pop("budget_json") or "{}")
            d["scripts"] = json.loads(d.pop("scripts_json") or "{}")
            return d
        finally:
            conn.close()

    def update_batch_status(self, batch_id: str, new_status: str) -> Tuple[bool, Optional[str]]:
        """严格按状态机迁移表检查并更新批次状态。"""
        now = int(time.time())
        completed_at = now if new_status in ("succeeded", "failed", "partial_failed", "cancelled") else None
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT status FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if not row:
                return False, f"批次 '{batch_id}' 不存在"

            cur_status_str = row["status"]
            try:
                cur_enum = BatchStatus(cur_status_str)
                new_enum = BatchStatus(new_status)
            except ValueError:
                return False, f"非法状态枚举值: {cur_status_str} -> {new_status}"

            # 校验状态迁移合法性
            allowed_next = BATCH_TRANSITIONS.get(cur_enum, set())
            if new_enum != cur_enum and new_enum not in allowed_next:
                return False, f"非法状态迁移: 当前为 '{cur_status_str}'，不允许跃迁至 '{new_status}' (合法目标: {[s.value for s in allowed_next]})"

            conn.execute("""
            UPDATE batches SET status = ?, updated_at = ?, completed_at = coalesce(?, completed_at)
            WHERE batch_id = ?
            """, (new_status, now, completed_at, batch_id))
            conn.commit()
            return True, None
        finally:
            conn.close()

    def update_batch_scripts(self, batch_id: str, scripts: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        now = int(time.time())
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT status FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if not row:
                return False, "批次不存在"
            if row["status"] in ("running", "queued", "succeeded", "failed", "partial_failed", "cancelled"):
                return False, f"批次处于 '{row['status']}' 状态，运行中或终态禁止修改脚本"

            conn.execute("""
            UPDATE batches SET scripts_json = ?, updated_at = ? WHERE batch_id = ?
            """, (json.dumps(scripts), now, batch_id))
            conn.commit()
            return True, None
        finally:
            conn.close()

    def update_draft_atomic(
        self,
        batch_id: str,
        scripts: Dict[str, Any],
        expected_revision: Optional[int] = None,
        is_generation: bool = False
    ) -> Tuple[bool, Optional[str], Optional[int]]:
        """
        原子修改草稿 (B01 & C02)：
        - 仅限 custom_ai 模式；fixed 模式严禁写入脚本。
        - 支持 expected_revision 乐观锁校验，防止旧生成结果覆盖或并发篡改。
        - 递增 revision，将批次与所有子任务状态同步流转为 pending_review。
        - 作废 (revoke) 既有已批准的授权记账，防二次利用。
        - 事务执行，失败必回滚并传播错误。
        """
        now = int(time.time())
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT status, mode, revision FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if not row:
                return False, f"批次 '{batch_id}' 不存在", None

            mode = row["mode"]
            if mode != "custom_ai":
                return False, "固定模式 (fixed) 严禁注入或编辑自定义脚本，模板不可变", None

            cur_status = row["status"]
            cur_rev = row["revision"] or 1
            if cur_status in ("running", "queued", "succeeded", "failed", "partial_failed", "cancelled"):
                return False, f"批次处于 '{cur_status}' 状态，运行中或终态禁止修改草稿", None

            if expected_revision is not None and cur_rev != expected_revision:
                return False, f"草稿版本冲突 (当前版本 {cur_rev} != 期望版本 {expected_revision})，已被其他操作修改", None

            # 校验状态迁移至 pending_review 是否合法
            cur_enum = BatchStatus(cur_status)
            allowed_next = BATCH_TRANSITIONS.get(cur_enum, set())
            if BatchStatus.PENDING_REVIEW != cur_enum and BatchStatus.PENDING_REVIEW not in allowed_next:
                return False, f"当前状态 '{cur_status}' 不允许迁移至 'pending_review'", None

            new_revision = cur_rev + 1

            # 1. 更新 batches 表 (脚本、状态、版本号)
            conn.execute("""
            UPDATE batches
            SET scripts_json = ?, status = ?, revision = ?, updated_at = ?
            WHERE batch_id = ?
            """, (json.dumps(scripts), BatchStatus.PENDING_REVIEW.value, new_revision, now, batch_id))

            # 2. 既有审批记录全部作废
            conn.execute("""
            UPDATE approvals
            SET status = 'revoked', consumed = 1, consumed_at = ?
            WHERE batch_id = ? AND status = 'approved'
            """, (now, batch_id))

            # 3. 同步重置子任务状态为 pending_review
            conn.execute("""
            UPDATE tasks
            SET status = ?, updated_at = ?
            WHERE batch_id = ?
            """, (TaskStatus.PENDING_REVIEW.value, now, batch_id))

            # 4. 追加审计日志
            conn.execute("""
            INSERT INTO events (batch_id, event_type, payload_json, created_at)
            VALUES (?, 'draft_updated_atomic', ?, ?)
            """, (batch_id, json.dumps({
                "new_revision": new_revision,
                "is_generation": is_generation,
                "message": "修改草稿原子完成：版本递增，状态转为待审核，既有审批全部作废"
            }), now))

            conn.commit()
            return True, None, new_revision
        except Exception as e:
            conn.rollback()
            return False, f"原子更新草稿事务失败: {e}", None
        finally:
            conn.close()

    def claim_batch_for_execution_atomic(
        self,
        batch_id: str,
        expected_revision: Optional[int] = None
    ) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        原子领取批次执行权 (C02)：
        - 同一事务内加排他锁判定 status == 'approved' 与 revision。
        - 单一原子 CAS 更新 status 为 'queued'，revision 自增 + 1。
        - 联动将所有关联子任务原子更新为 'queued'。
        - 仅有一个并发请求能够成功进入 queued，杜绝并发调用重复派发执行两次。
        """
        now = int(time.time())
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, mode, revision, device_id, app_ids_json, filters_json, budget_json, scripts_json FROM batches WHERE batch_id = ?",
                (batch_id,)
            ).fetchone()
            if not row:
                conn.rollback()
                return False, f"批次 '{batch_id}' 不存在", None

            cur_status = row["status"]
            cur_rev = row["revision"] or 1
            mode = row["mode"]

            if cur_status != BatchStatus.APPROVED.value:
                conn.rollback()
                return False, f"批次未处于待执行批准状态 (当前状态: '{cur_status}')，禁止派发", None

            if expected_revision is not None and cur_rev != expected_revision:
                conn.rollback()
                return False, f"批次版本冲突 (当前版本 {cur_rev} != 期望版本 {expected_revision})，已被其他操作修改", None

            new_rev = cur_rev + 1
            cursor = conn.execute("""
                UPDATE batches
                SET status = 'queued', revision = ?, updated_at = ?
                WHERE batch_id = ? AND status = 'approved' AND revision = ?
            """, (new_rev, now, batch_id, cur_rev))

            if cursor.rowcount == 0:
                conn.rollback()
                return False, "并发竞态冲突：批次已被并发请求率先领取执行", None

            # 同步原子更新子任务
            conn.execute("""
                UPDATE tasks
                SET status = 'queued', updated_at = ?
                WHERE batch_id = ?
            """, (now, batch_id))

            conn.commit()
            batch_data = dict(row)
            batch_data["revision"] = new_rev
            batch_data["status"] = "queued"
            batch_data["app_ids"] = json.loads(batch_data.pop("app_ids_json"))
            batch_data["filters"] = json.loads(batch_data.pop("filters_json") or "{}")
            batch_data["budget"] = json.loads(batch_data.pop("budget_json") or "{}")
            batch_data["scripts"] = json.loads(batch_data.pop("scripts_json") or "{}")
            return True, None, batch_data
        except Exception as e:
            conn.rollback()
            return False, f"原子领取批次失败: {e}", None
        finally:
            conn.close()

    def cancel_batch_atomic(
        self,
        batch_id: str,
        reason: str = "用户取消",
        operator: str = "admin"
    ) -> Tuple[bool, Optional[str]]:
        """原子取消批次及所有在途子任务 (C03)。"""
        now = int(time.time())
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if not row:
                conn.rollback()
                return False, f"批次 '{batch_id}' 不存在"

            cur_status = row["status"]
            if cur_status in ("succeeded", "failed", "partial_failed", "cancelled"):
                conn.rollback()
                return False, f"批次已处于终态 '{cur_status}'，无法取消"

            # 区分取消请求态与停止确认终态 (D01)
            # 运行中批次先进入 cancel_requested，待 Worker 停止并返回回执后确认为 cancelled；
            # 排队中/草稿/待审核等未向真机下发指令的批次可直接终态 cancelled。
            if cur_status == "running":
                target_batch_status = "cancel_requested"
                target_task_status = "cancel_requested"
                completed_at = None
            else:
                target_batch_status = "cancelled"
                target_task_status = "cancelled"
                completed_at = now

            conn.execute("""
                UPDATE batches SET status = ?, updated_at = ?, completed_at = ? WHERE batch_id = ?
            """, (target_batch_status, now, completed_at, batch_id))

            conn.execute("""
                UPDATE tasks SET status = ?, updated_at = ?
                WHERE batch_id = ? AND status IN ('draft', 'pending_review', 'approved', 'queued', 'running')
            """, (target_task_status, now, batch_id))

            conn.execute("""
                INSERT INTO events (batch_id, event_type, payload_json, created_at)
                VALUES (?, 'batch_cancelled', ?, ?)
            """, (batch_id, json.dumps({"operator": operator, "reason": reason, "target_status": target_batch_status}), now))

            conn.commit()
            return True, None
        except Exception as e:
            conn.rollback()
            return False, f"取消批次失败: {e}"
        finally:
            conn.close()

    def finalize_cancellation_atomic(self, batch_id: str) -> None:
        """当 Worker 停止确认并返回回执后，将 cancel_requested 转为终态 cancelled (D01)。"""
        now = int(time.time())
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                UPDATE batches SET status = 'cancelled', updated_at = ?, completed_at = ?
                WHERE batch_id = ? AND status = 'cancel_requested'
            """, (now, now, batch_id))
            conn.execute("""
                UPDATE tasks SET status = 'cancelled', updated_at = ?
                WHERE batch_id = ? AND status = 'cancel_requested'
            """, (now, batch_id))
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            conn.close()

    def list_batches(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["app_ids"] = json.loads(d.pop("app_ids_json"))
                d["filters"] = json.loads(d.pop("filters_json") or "{}")
                d["budget"] = json.loads(d.pop("budget_json") or "{}")
                d["scripts"] = json.loads(d.pop("scripts_json") or "{}")
                res.append(d)
            return res
        finally:
            conn.close()

    # --- Task Operations ---

    def create_task(
        self,
        task_id: str,
        batch_id: str,
        app_id: str,
        device_id: str,
        status: str,
        attempt_id: str,
        adapter_version: str = "1.0.0"
    ) -> None:
        now = int(time.time())
        conn = self._get_connection()
        try:
            conn.execute("""
            INSERT INTO tasks (
                task_id, batch_id, app_id, device_id, status, attempt_id, adapter_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (task_id, batch_id, app_id, device_id, status, attempt_id, adapter_version, now, now))
            conn.commit()
        finally:
            conn.close()

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["receipt"] = json.loads(d.pop("receipt_json") or "null")
            return d
        finally:
            conn.close()

    def list_tasks_for_batch(self, batch_id: str) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE batch_id = ? ORDER BY created_at ASC", (batch_id,)
            ).fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["receipt"] = json.loads(d.pop("receipt_json") or "null")
                res.append(d)
            return res
        finally:
            conn.close()

    def update_tasks_status_for_batch(self, batch_id: str, new_status: str) -> None:
        """批量更新批次下所有子任务状态。"""
        now = int(time.time())
        conn = self._get_connection()
        try:
            conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE batch_id = ?", (new_status, now, batch_id))
            conn.commit()
        finally:
            conn.close()

    def start_task_attempt(self, task_id: str, attempt_id: str) -> bool:
        """为任务开启新的执行 attempt，并流转至 RUNNING。"""
        now = int(time.time())
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return False
            cur_status = TaskStatus(row["status"])
            if cur_status not in (TaskStatus.QUEUED, TaskStatus.APPROVED, TaskStatus.RUNNING):
                return False
            conn.execute("""
            UPDATE tasks SET status = ?, attempt_id = ?, updated_at = ?
            WHERE task_id = ?
            """, (TaskStatus.RUNNING.value, attempt_id, now, task_id))
            conn.commit()
            return True
        finally:
            conn.close()

    def save_task_receipt(
        self,
        task_id: str,
        status: str,
        attempt_id: str,
        receipt: Dict[str, Any],
        error: Optional[str] = None
    ) -> bool:
        """只允许最新的 attempt 写入回执，并严格校验状态流转。"""
        now = int(time.time())
        completed_at = now if status in ("succeeded", "failed", "timed_out", "cancelled") else None
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT attempt_id, status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return False
            if row["attempt_id"] != attempt_id:
                # 拒绝过期 attempt 回执
                return False

            cur_status_str = row["status"]
            try:
                cur_enum = TaskStatus(cur_status_str)
                new_enum = TaskStatus(status)
                allowed_next = TASK_TRANSITIONS.get(cur_enum, set())
                if new_enum != cur_enum and new_enum not in allowed_next:
                    return False
            except ValueError:
                return False

            conn.execute("""
            UPDATE tasks SET status = ?, receipt_json = ?, error = ?, updated_at = ?, completed_at = coalesce(?, completed_at)
            WHERE task_id = ? AND attempt_id = ?
            """, (status, json.dumps(receipt), error, now, completed_at, task_id, attempt_id))
            conn.commit()
            return True
        finally:
            conn.close()

    # --- Approval Operations (原子 CAS 消费) ---

    def save_approval(self, approval_data: Dict[str, Any]) -> None:
        conn = self._get_connection()
        try:
            conn.execute("""
            INSERT OR REPLACE INTO approvals (
                batch_id, status, reviewer_id, canonical_hash, consumed,
                approved_at, expires_at, consumed_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                approval_data["batch_id"],
                approval_data["status"],
                approval_data["reviewer_id"],
                approval_data["canonical_hash"],
                1 if approval_data.get("consumed") else 0,
                approval_data["approved_at"],
                approval_data["expires_at"],
                approval_data.get("consumed_at"),
                json.dumps(approval_data)
            ))
            conn.commit()
        finally:
            conn.close()

    def get_approval(self, batch_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT * FROM approvals WHERE batch_id = ?", (batch_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["consumed"] = bool(d["consumed"])
            return d
        finally:
            conn.close()

    def consume_approval_atomic(self, batch_id: str, expected_hash: str) -> Tuple[bool, Optional[str]]:
        """在单条 SQL 事务内原子执行 CAS 消费授权，杜绝并发竞争与重复消费。"""
        now = int(time.time())
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE approvals
            SET consumed = 1, consumed_at = ?
            WHERE batch_id = ?
              AND canonical_hash = ?
              AND consumed = 0
              AND status = 'approved'
              AND expires_at > ?
            """, (now, batch_id, expected_hash, now))

            if cursor.rowcount == 1:
                conn.commit()
                return True, None

            # 若更新行数为 0，诊断失败原因
            row = conn.execute("SELECT * FROM approvals WHERE batch_id = ?", (batch_id,)).fetchone()
            if not row:
                return False, "未找到该批次的审批记录，禁止执行"
            if row["status"] != "approved":
                return False, f"审批状态不为 approved (当前: {row['status']})"
            if row["consumed"]:
                return False, "审批授权已被消费（一次性授权），禁止重复执行"
            if row["canonical_hash"] != expected_hash:
                return False, "脚本或参数在审批后被修改，哈希校验不符，审批已失效"
            if now > row["expires_at"]:
                return False, "审批授权已过期，请重新发起审核"

            return False, "原子授权消费失败"
        finally:
            conn.close()

    # --- Events Operations ---

    def append_event(self, batch_id: str, event_type: str, payload: Dict[str, Any], task_id: Optional[str] = None) -> None:
        now = int(time.time())
        conn = self._get_connection()
        try:
            conn.execute("""
            INSERT INTO events (batch_id, task_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """, (batch_id, task_id, event_type, json.dumps(payload), now))
            conn.commit()
        finally:
            conn.close()

    def list_events(self, batch_id: str, since_id: int = 0) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        try:
            rows = conn.execute("""
            SELECT * FROM events WHERE batch_id = ? AND id > ? ORDER BY id ASC
            """, (batch_id, since_id)).fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d.pop("payload_json"))
                res.append(d)
            return res
        finally:
            conn.close()
