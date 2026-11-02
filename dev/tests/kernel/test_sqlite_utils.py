"""
全局 SQLite 并发治理与健康巡检回归测试 (dev/tests/kernel/test_sqlite_utils.py)。
"""

import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from lite_agent.core.sqlite_utils import (
    SafeSqliteConnection,
    _PYSQLITE_GLOBAL_LOCK,
    connect_sqlite,
    safe_chromadb_sqlite_env,
)


def test_connect_sqlite_wal_busy_timeout_and_close_on_exit(tmp_path: Path):
    db_file = tmp_path / "test_wal.db"
    with connect_sqlite(
        db_file,
        row_factory=sqlite3.Row,
        foreign_keys=True,
        close_on_exit=True,
    ) as conn:
        assert isinstance(conn, SafeSqliteConnection)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert int(fk) == 1
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert int(busy) == 10000
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items (name) VALUES (?)", ("alpha",))

    # 退出 with 块后连接必须已自动关闭
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT * FROM items")

    # 数据已自动提交并持久化
    with connect_sqlite(db_file, row_factory=sqlite3.Row, close_on_exit=True) as conn2:
        row = conn2.execute("SELECT name FROM items WHERE id=1").fetchone()
        assert row is not None and row["name"] == "alpha"


def test_shared_connection_thread_safe_concurrent_writes(tmp_path: Path):
    db_file = tmp_path / "shared_conn.db"
    conn = connect_sqlite(db_file, check_same_thread=False)
    try:
        conn.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY AUTOINCREMENT, worker INT)")
        conn.commit()

        errors = []

        def worker(w_id: int):
            try:
                for _ in range(25):
                    conn.execute("INSERT INTO counter (worker) VALUES (?)", (w_id,))
                    conn.commit()
                    conn.execute("SELECT COUNT(*) FROM counter").fetchone()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"并发共享连接出现异常: {errors}"
        total = conn.execute("SELECT COUNT(*) FROM counter").fetchone()[0]
        assert total == 6 * 25
    finally:
        conn.close()


def test_safe_chromadb_sqlite_env_restores_stdlib_sqlite3():
    orig = sys.modules.get("sqlite3")
    with safe_chromadb_sqlite_env():
        pass
    assert sys.modules.get("sqlite3") is orig
    assert isinstance(_PYSQLITE_GLOBAL_LOCK, type(threading.RLock()))


def test_health_report_defines_svc_and_does_not_crash():
    """回归：23:50 每日健康巡检广播不得因 svc 未定义抛出 NameError。"""
    from lite_agent.features.sys_ops.health_service import get_health_report

    with patch("urllib.request.urlopen", side_effect=OSError("offline test")), \
         patch("lite_agent.features.sys_ops.llm_ops_service.check_deepseek_balance", return_value="余额: ¥100.00"):
        report = get_health_report()

    assert "Lite Agent 系统全方位健康自检报告" in report
    assert "Systemd 守护进程" in report
    assert "记忆蒸馏复盘" in report
