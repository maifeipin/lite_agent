"""
全局 SQLite 并发安全与连接治理工具 (lite_agent.core.sqlite_utils)。

集中解决两类跨模块 SQLite 并发隐患：
1. 标准库 sqlite3 多线程/多任务并发读写同一数据库时的锁竞争、连接泄漏与事务重入：
   - 统一默认 timeout (>=10s) 与 PRAGMA busy_timeout (10000ms)
   - 对可写文件库统一开启 PRAGMA journal_mode=WAL 与 synchronous=NORMAL
   - SafeSqliteConnection 提供实例级 RLock 串行化单连接跨线程操作，并支持
     close_on_exit=True 在退出 `with conn:` 块时自动 close()，彻底消除隐式连接泄漏。
2. ChromaDB 依赖的 pysqlite3-binary (捆绑 SQLite 3.51.1) 在多线程并发 connect/close
   WAL 数据库时触发 unixBigLock 与 pInode->pLockMutex 的 C 层 AB-BA 死锁：
   - 通过进程级唯一的 _PYSQLITE_GLOBAL_LOCK 包装 pysqlite3.connect 与 Connection.close
   - 通过 safe_chromadb_sqlite_env() 上下文管理器受控注入并恢复 sys.modules['sqlite3']，
     避免各业务模块重复内联补丁或各自持有独立锁。
"""

from contextlib import contextmanager
import os
import sqlite3 as _stdlib_sqlite3
import sys
import threading
from typing import Any, Optional, Union
from urllib.parse import parse_qs, urlparse

# 进程级全局锁：串行化底层连接 open/close 生命周期与 pysqlite3 C 扩展互斥
_SQLITE_LIFECYCLE_LOCK = threading.RLock()
_PYSQLITE_GLOBAL_LOCK = threading.RLock()
_CHROMADB_ENV_LOCK = threading.RLock()


class SafeSqliteConnection(_stdlib_sqlite3.Connection):
    """线程安全且支持退出上下文自动关闭的 SQLite Connection 子类。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._op_lock = threading.RLock()
        self._close_on_exit = False
        self._is_closed = False

    def execute(self, sql: str, parameters: Any = (), /) -> _stdlib_sqlite3.Cursor:
        with self._op_lock:
            return super().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> _stdlib_sqlite3.Cursor:
        with self._op_lock:
            return super().executemany(sql, parameters)

    def executescript(self, sql_script: str, /) -> _stdlib_sqlite3.Cursor:
        with self._op_lock:
            return super().executescript(sql_script)

    def commit(self) -> None:
        with self._op_lock:
            return super().commit()

    def rollback(self) -> None:
        with self._op_lock:
            return super().rollback()

    def close(self) -> None:
        with self._op_lock:
            if self._is_closed:
                return
            self._is_closed = True
            with _SQLITE_LIFECYCLE_LOCK:
                return super().close()

    def __enter__(self) -> "SafeSqliteConnection":
        with self._op_lock:
            super().__enter__()
            return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        with self._op_lock:
            try:
                return bool(super().__exit__(exc_type, exc_val, exc_tb))
            finally:
                if self._close_on_exit:
                    self.close()


def _is_readonly_or_memory(database: Union[str, os.PathLike], uri: bool = False) -> bool:
    try:
        db_str = os.fspath(database)
    except Exception:
        return True
    if not isinstance(db_str, str) or not db_str or db_str == ":memory:":
        return True
    if uri and db_str.startswith("file:"):
        parsed = urlparse(db_str)
        qs = parse_qs(parsed.query)
        if qs.get("mode") in (["ro"], ["memory"]):
            return True
    return False


def connect_sqlite(
    database: Union[str, os.PathLike],
    *,
    timeout: float = 10.0,
    row_factory: Any = None,
    wal: Optional[bool] = None,
    busy_timeout_ms: int = 10000,
    foreign_keys: bool = False,
    close_on_exit: bool = False,
    **kwargs: Any,
) -> _stdlib_sqlite3.Connection:
    """
    全局统一 SQLite 连接工厂。

    参数:
      - database: 数据库文件路径、PathLike 或 ':memory:' / URI
      - timeout: Python 层锁等待超时秒数（默认最小提升至 10.0s，除非显式传入更大值）
      - row_factory: 可选行工厂（如 sqlite3.Row）
      - wal: 是否开启 WAL 模式（None 表示对非只读文件库自动开启）
      - busy_timeout_ms: SQLite C 层 busy_timeout 毫秒数（默认 10000ms）
      - foreign_keys: 是否开启 PRAGMA foreign_keys=ON
      - close_on_exit: 当作为 `with connect_sqlite(...) as conn:` 使用时，退出 with 块是否自动 close()
    """
    effective_timeout = max(float(timeout if timeout is not None else 10.0), 5.0)
    is_uri = bool(kwargs.get("uri", False))
    is_ro_or_mem = _is_readonly_or_memory(database, uri=is_uri)
    enable_wal = (not is_ro_or_mem) if wal is None else (bool(wal) and not is_ro_or_mem)

    kwargs.setdefault("factory", SafeSqliteConnection)

    with _SQLITE_LIFECYCLE_LOCK:
        conn = _stdlib_sqlite3.connect(database, timeout=effective_timeout, **kwargs)

    if isinstance(conn, SafeSqliteConnection):
        conn._close_on_exit = bool(close_on_exit)

    if row_factory is not None:
        conn.row_factory = row_factory

    try:
        if busy_timeout_ms and busy_timeout_ms > 0:
            conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if enable_wal:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        # 只读库或受限连接上 PRAGMA 可能受限，保持连接可用
        pass

    return conn


def _ensure_patched_pysqlite3() -> Optional[Any]:
    """确保 pysqlite3 模块全局仅打一次线程安全锁补丁，并常驻 sys.modules['pysqlite3']。"""
    with _PYSQLITE_GLOBAL_LOCK:
        try:
            import pysqlite3 as _pysqlite3
        except ImportError:
            return None

        if not getattr(_pysqlite3, "_lite_agent_safe_patched", False):
            _orig_connect = _pysqlite3.connect

            class _SafePySqliteConnection(_pysqlite3.Connection):
                def close(self) -> None:
                    with _PYSQLITE_GLOBAL_LOCK:
                        return super().close()

            def _safe_pysqlite_connect(*args: Any, **kwargs: Any) -> Any:
                kwargs.setdefault("factory", _SafePySqliteConnection)
                with _PYSQLITE_GLOBAL_LOCK:
                    return _orig_connect(*args, **kwargs)

            _pysqlite3.connect = _safe_pysqlite_connect
            _pysqlite3._lite_agent_safe_patched = True

        return _pysqlite3


@contextmanager
def safe_chromadb_sqlite_env():
    """
    为 ChromaDB 导入与初始化提供受控的 pysqlite3 上下文环境：
    1. 解决 Debian/CentOS 系统自带旧版 sqlite3 (<3.35) 不满足 ChromaDB 要求的问题；
    2. 全局共享同一个 _PYSQLITE_GLOBAL_LOCK 防止 pysqlite3 (SQLite 3.51.1) 多线程 WAL 死锁；
    3. 退出上下文时强制恢复标准库 sqlite3，绝不污染业务模块。
    """
    with _CHROMADB_ENV_LOCK:
        _orig_sqlite3 = sys.modules.get("sqlite3", _stdlib_sqlite3)
        _pysqlite3 = _ensure_patched_pysqlite3()
        if _pysqlite3 is not None:
            sys.modules["sqlite3"] = _pysqlite3
        try:
            yield _pysqlite3
        finally:
            if _orig_sqlite3 is not None:
                sys.modules["sqlite3"] = _orig_sqlite3

