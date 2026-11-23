"""
全局 SQLite 并发治理与健康巡检回归测试 (dev/tests/kernel/test_sqlite_utils.py)。
使用标准库 unittest 编写，不依赖第三方 pytest 包即可独立运行。
"""

from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from lite_agent.core.sqlite_utils import (
    SafeSqliteConnection,
    SafeSqliteCursor,
    _PYSQLITE_GLOBAL_LOCK,
    connect_sqlite,
    safe_chromadb_sqlite_env,
)


class TestSqliteUtils(unittest.TestCase):

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="sqlite_utils_test_")
        self.tmp_path = Path(self._tmp_dir.name)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_01_connect_sqlite_wal_busy_timeout_and_close_on_exit(self):
        db_file = self.tmp_path / "test_wal.db"
        with connect_sqlite(
            db_file,
            row_factory=sqlite3.Row,
            foreign_keys=True,
            close_on_exit=True,
        ) as conn:
            self.assertIsInstance(conn, SafeSqliteConnection)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal")
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(int(fk), 1)
            busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(int(busy), 10000)
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO items (name) VALUES (?)", ("alpha",))

        # 退出 with 块后连接必须已自动关闭
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT * FROM items")

        # 数据已自动提交并持久化
        with connect_sqlite(db_file, row_factory=sqlite3.Row, close_on_exit=True) as conn2:
            row = conn2.execute("SELECT name FROM items WHERE id=1").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["name"], "alpha")

    def test_02_explicit_long_timeout_preserved_in_busy_timeout(self):
        """回归 [P2]: connect_sqlite(timeout=30) 必须将 busy_timeout 设为 30000ms 而非覆盖为 10000ms。"""
        db_file = self.tmp_path / "timeout30.db"
        with connect_sqlite(db_file, timeout=30.0, close_on_exit=True) as conn:
            busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(int(busy), 30000)

    def test_03_shared_connection_transaction_isolation_with_context_manager(self):
        """回归 [P1]: 共享连接上线程 B 回滚不得撤销线程 A 在事务中写入的数据 (with conn 模式)。"""
        db_file = self.tmp_path / "shared_tx_ctx.db"
        conn = connect_sqlite(db_file, check_same_thread=False)
        try:
            with conn:
                conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, val TEXT)")

            a_inserted = threading.Event()
            b_finished = threading.Event()

            def thread_a():
                with conn:
                    conn.execute("INSERT INTO items (val) VALUES ('from_A')")
                    a_inserted.set()
                    # 保持事务开启一小段时间，验证线程 B 无法在中途插入或回滚 A 的事务
                    time.sleep(0.08)

            def thread_b():
                a_inserted.wait(timeout=2.0)
                try:
                    with conn:
                        conn.execute("INSERT INTO items (val) VALUES ('from_B')")
                        raise RuntimeError("trigger rollback in B")
                except RuntimeError:
                    pass
                finally:
                    b_finished.set()

            ta = threading.Thread(target=thread_a)
            tb = threading.Thread(target=thread_b)
            ta.start()
            tb.start()
            ta.join(timeout=5.0)
            tb.join(timeout=5.0)
            self.assertTrue(b_finished.is_set())

            rows = [r[0] for r in conn.execute("SELECT val FROM items").fetchall()]
            self.assertEqual(rows, ["from_A"])
        finally:
            conn.close()

    def test_04_shared_connection_transaction_isolation_raw_execute_commit(self):
        """回归 [P1]: 共享连接上即使直接调用 execute() + commit()/rollback()，未提交事务期间也必须独占连接锁。"""
        db_file = self.tmp_path / "shared_tx_raw.db"
        conn = connect_sqlite(db_file, check_same_thread=False)
        try:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, val TEXT)")
            conn.commit()

            a_inserted = threading.Event()

            def thread_a():
                conn.execute("INSERT INTO items (val) VALUES ('from_A_raw')")
                a_inserted.set()
                time.sleep(0.08)
                conn.commit()

            def thread_b():
                a_inserted.wait(timeout=2.0)
                # 线程 B 尝试回滚：必须等待线程 A commit 完成后才能获得锁，不能撤销 A 的写入
                conn.rollback()

            ta = threading.Thread(target=thread_a)
            tb = threading.Thread(target=thread_b)
            ta.start()
            tb.start()
            ta.join(timeout=5.0)
            tb.join(timeout=5.0)

            rows = [r[0] for r in conn.execute("SELECT val FROM items").fetchall()]
            self.assertEqual(rows, ["from_A_raw"])
        finally:
            conn.close()

    def test_05_wal_failure_does_not_skip_foreign_keys_and_fk_failure_raises(self):
        """回归 [P1]: WAL 初始化失败时仍必须启用 foreign_keys=True；若外键启用失败必须关闭连接并抛错。"""
        db_file = self.tmp_path / "locked_wal.db"
        # 先用一个普通连接以排他事务锁住新库，使后续连接的 PRAGMA journal_mode=WAL 失败
        blocker = sqlite3.connect(db_file, timeout=0.1, isolation_level=None)
        try:
            blocker.execute("BEGIN EXCLUSIVE")
            blocker.execute("CREATE TABLE t (id INT)")

            conn = connect_sqlite(
                db_file,
                timeout=0.05,
                busy_timeout_ms=20,
                foreign_keys=True,
            )
            try:
                fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                self.assertEqual(int(fk), 1, "WAL 切换失败后 foreign_keys=ON 仍必须生效")
            finally:
                conn.close()
        finally:
            blocker.rollback()
            blocker.close()

        # 若 PRAGMA foreign_keys=ON 自身无法生效，connect_sqlite 必须关闭连接并抛异常
        class _BrokenFkConnection(SafeSqliteConnection):
            def execute(self, sql: str, parameters=(), /):
                if "PRAGMA foreign_keys=ON" in sql:
                    return super().execute("PRAGMA foreign_keys=OFF", parameters)
                return super().execute(sql, parameters)

        with self.assertRaises(sqlite3.OperationalError):
            connect_sqlite(db_file, foreign_keys=True, factory=_BrokenFkConnection)

    def test_06_safe_chromadb_sqlite_env_restores_stdlib_sqlite3(self):
        orig = sys.modules.get("sqlite3")
        with safe_chromadb_sqlite_env():
            pass
        self.assertIs(sys.modules.get("sqlite3"), orig)
        self.assertIsInstance(_PYSQLITE_GLOBAL_LOCK, type(threading.RLock()))

    def test_07_health_report_defines_svc_and_does_not_crash(self):
        """回归：23:50 每日健康巡检广播不得因 svc 未定义抛出 NameError。"""
        from lite_agent.features.sys_ops.health_service import get_health_report

        with patch("urllib.request.urlopen", side_effect=OSError("offline test")), \
             patch("lite_agent.features.sys_ops.llm_ops_service.check_deepseek_balance", return_value="余额: ¥100.00"):
            report = get_health_report()

        self.assertIn("Lite Agent 系统全方位健康自检报告", report)
        self.assertIn("Systemd 守护进程", report)
        self.assertIn("记忆蒸馏复盘", report)

    def test_08_memory_store_concurrent_writes_and_rollback_isolation(self):
        """回归 [P1]: MemoryStore 跨线程并发调用 save_message/touch_user/mark_distilled 与异常回滚互不串扰。"""
        from lite_agent.memory_engine.store import MemoryStore

        db_file = self.tmp_path / "memory_test.db"
        store = MemoryStore(db_path=str(db_file))
        try:
            errors = []

            def writer(idx: int):
                try:
                    for j in range(10):
                        rid = store.save_message(
                            speaker_id=f"u_{idx}",
                            speaker_nick=f"User{idx}",
                            content=f"hello {idx}-{j}",
                            role="user",
                        )
                        store.touch_user(f"u_{idx}", f"User{idx}")
                        store.update_importance(rid, 0.1)
                except Exception as exc:
                    errors.append(exc)

            def failing_tx():
                for _ in range(10):
                    try:
                        with store.conn:
                            store.conn.execute(
                                "INSERT INTO conversations (speaker_id, role, content, created_at) VALUES (?,?,?,?)",
                                ("rollback_user", "user", "should_rollback", time.time()),
                            )
                            raise ValueError("abort transaction")
                    except ValueError:
                        pass

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
            threads.append(threading.Thread(target=failing_tx))
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

            self.assertEqual(errors, [])
            cnt_valid = store.conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE speaker_id LIKE 'u_%'"
            ).fetchone()[0]
            cnt_rolled_back = store.conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE speaker_id = 'rollback_user'"
            ).fetchone()[0]
            self.assertEqual(cnt_valid, 4 * 10)
            self.assertEqual(cnt_rolled_back, 0)
        finally:
            store.close()

    def test_09_sql_exception_paths_sync_tx_lock(self):
        """回归 [P1]: executemany 异常开启事务必须持锁；executescript 异常结束事务必须释放锁。"""
        db_file = self.tmp_path / "sql_exc_sync.db"
        conn = connect_sqlite(db_file, check_same_thread=False)
        try:
            conn.execute("CREATE TABLE uniq_items (val TEXT UNIQUE)")
            conn.commit()

            # 1) executemany 第 1 条成功、第 2 条触发 UNIQUE 异常：in_transaction=True，必须持有事务锁
            exc_caught = threading.Event()
            b_done = threading.Event()

            def thread_a_partial():
                try:
                    conn.executemany(
                        "INSERT INTO uniq_items (val) VALUES (?)",
                        [("first_ok",), ("first_ok",)],
                    )
                except sqlite3.IntegrityError:
                    self.assertTrue(conn.in_transaction)
                    self.assertTrue(conn._tx_lock_held)
                    exc_caught.set()
                    time.sleep(0.08)
                    conn.commit()

            def thread_b_try_rollback():
                exc_caught.wait(timeout=2.0)
                conn.rollback()
                b_done.set()

            ta = threading.Thread(target=thread_a_partial)
            tb = threading.Thread(target=thread_b_try_rollback)
            ta.start()
            tb.start()
            ta.join(timeout=5.0)
            tb.join(timeout=5.0)
            self.assertTrue(b_done.is_set())

            rows = [r[0] for r in conn.execute("SELECT val FROM uniq_items").fetchall()]
            self.assertEqual(rows, ["first_ok"], "executemany 部分插入的数据在提交前不得被其他线程回滚")

            # 2) executescript 隐式提交已有事务后遇到语法错误：in_transaction=False，必须释放事务锁
            conn.execute("INSERT INTO uniq_items (val) VALUES ('second_ok')")
            self.assertTrue(conn.in_transaction)
            self.assertTrue(conn._tx_lock_held)

            with self.assertRaises(sqlite3.OperationalError):
                conn.executescript("INSERT INTO uniq_items (val) VALUES ('third_ok'); INVALID SQL SYNTAX;")

            self.assertFalse(conn.in_transaction)
            self.assertFalse(conn._tx_lock_held)

            # 验证其他线程不会被死锁阻塞
            other_thread_ok = threading.Event()

            def thread_c_verify_not_blocked():
                conn.execute("INSERT INTO uniq_items (val) VALUES ('from_c')")
                conn.commit()
                other_thread_ok.set()

            tc = threading.Thread(target=thread_c_verify_not_blocked)
            tc.start()
            tc.join(timeout=2.0)
            self.assertTrue(other_thread_ok.is_set(), "executescript 异常结束事务后不得阻塞其他线程")
        finally:
            conn.close()

    def test_10_shortcut_methods_return_safe_cursor(self):
        """回归 [P1]: conn.execute/executemany/executescript 必须返回 SafeSqliteCursor，复用游标不得绕过事务锁。"""
        db_file = self.tmp_path / "shortcut_cursor.db"
        conn = connect_sqlite(db_file, check_same_thread=False)
        try:
            cur = conn.execute("CREATE TABLE t_cur (val TEXT)")
            self.assertIsInstance(cur, SafeSqliteCursor)
            conn.commit()

            reused_inserted = threading.Event()

            def thread_a():
                # 复用 conn.execute() 返回的游标发起写入
                cur.execute("INSERT INTO t_cur (val) VALUES ('via_reused_cursor')")
                self.assertTrue(conn.in_transaction)
                self.assertTrue(conn._tx_lock_held)
                reused_inserted.set()
                time.sleep(0.08)
                conn.commit()

            def thread_b():
                reused_inserted.wait(timeout=2.0)
                conn.rollback()

            ta = threading.Thread(target=thread_a)
            tb = threading.Thread(target=thread_b)
            ta.start()
            tb.start()
            ta.join(timeout=5.0)
            tb.join(timeout=5.0)

            rows = [r[0] for r in cur.execute("SELECT val FROM t_cur").fetchall()]
            self.assertEqual(rows, ["via_reused_cursor"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
