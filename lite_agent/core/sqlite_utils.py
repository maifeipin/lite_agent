"""
全局 SQLite 并发安全与连接治理工具 (lite_agent.core.sqlite_utils)。

集中解决两类跨模块 SQLite 并发隐患：
1. 标准库 sqlite3 多线程/多任务并发读写同一数据库时的锁竞争、连接泄漏与事务串扰：
   - 统一默认 timeout (>=10s) 与匹配的 PRAGMA busy_timeout (默认跟随 timeout*1000ms，保留更长显式超时)
   - 对可写文件库独立尝试开启 PRAGMA journal_mode=WAL 与 synchronous=NORMAL（失败不阻断后续约束）
   - 对 foreign_keys=True 独立执行并强校验 PRAGMA foreign_keys=1，若开启失败则立即关闭连接并抛错
   - SafeSqliteConnection 提供事务级 RLock 隔离：无论通过 `with conn:` 还是 `execute() + commit()/rollback()`，
     均在完整事务周期内持有 `_op_lock`，防止多线程共享连接时回滚或提交互相串扰；同时支持
     close_on_exit=True 在退出最外层 `with conn:` 块时自动 close()，消除短连接泄漏。
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


class SafeSqliteCursor(_stdlib_sqlite3.Cursor):
    """线程安全的 Cursor 子类，确保通过 cursor.execute 发起的事务同样纳入连接级事务锁保护。"""

    def execute(self, sql: str, parameters: Any = (), /) -> "SafeSqliteCursor":
        conn = self.connection
        if isinstance(conn, SafeSqliteConnection):
            with conn._op_lock:
                res = super().execute(sql, parameters)
                conn._sync_tx_lock()
                return res
        return super().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> "SafeSqliteCursor":
        conn = self.connection
        if isinstance(conn, SafeSqliteConnection):
            with conn._op_lock:
                res = super().executemany(sql, parameters)
                conn._sync_tx_lock()
                return res
        return super().executemany(sql, parameters)

    def executescript(self, sql_script: str, /) -> "SafeSqliteCursor":
        conn = self.connection
        if isinstance(conn, SafeSqliteConnection):
            with conn._op_lock:
                res = super().executescript(sql_script)
                conn._sync_tx_lock()
                return res
        return super().executescript(sql_script)


class SafeSqliteConnection(_stdlib_sqlite3.Connection):
    """线程安全、事务级隔离且支持退出上下文自动关闭的 SQLite Connection 子类。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._op_lock = threading.RLock()
        self._close_on_exit = False
        self._is_closed = False
        self._tx_lock_held = False
        self._enter_depth = 0

    def _sync_tx_lock(self) -> None:
        """同步事务级锁持有状态：当连接进入未提交事务时持续持有 _op_lock，直至 commit/rollback/close。"""
        if self._is_closed:
            if self._tx_lock_held:
                self._tx_lock_held = False
                self._op_lock.release()
            return
        try:
            in_tx = bool(self.in_transaction)
        except _stdlib_sqlite3.ProgrammingError:
            in_tx = False
        if in_tx and not self._tx_lock_held:
            self._op_lock.acquire()
            self._tx_lock_held = True
        elif not in_tx and self._tx_lock_held:
            self._tx_lock_held = False
            self._op_lock.release()

    def cursor(self, factory: Any = SafeSqliteCursor) -> _stdlib_sqlite3.Cursor:
        with self._op_lock:
            return super().cursor(factory)

    def execute(self, sql: str, parameters: Any = (), /) -> _stdlib_sqlite3.Cursor:
        with self._op_lock:
            cur = super().execute(sql, parameters)
            self._sync_tx_lock()
            return cur

    def executemany(self, sql: str, parameters: Any, /) -> _stdlib_sqlite3.Cursor:
        with self._op_lock:
            cur = super().executemany(sql, parameters)
            self._sync_tx_lock()
            return cur

    def executescript(self, sql_script: str, /) -> _stdlib_sqlite3.Cursor:
        with self._op_lock:
            cur = super().executescript(sql_script)
            self._sync_tx_lock()
            return cur

    def commit(self) -> None:
        with self._op_lock:
            try:
                return super().commit()
            finally:
                self._sync_tx_lock()

    def rollback(self) -> None:
        with self._op_lock:
            try:
                return super().rollback()
            finally:
                self._sync_tx_lock()

    def close(self) -> None:
        with self._op_lock:
            if self._is_closed:
                return
            self._is_closed = True
            self._sync_tx_lock()
            with _SQLITE_LIFECYCLE_LOCK:
                return super().close()

    def __enter__(self) -> "SafeSqliteConnection":
        self._op_lock.acquire()
        try:
            super().__enter__()
            self._enter_depth += 1
            self._sync_tx_lock()
            return self
        except Exception:
            self._op_lock.release()
            raise

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        try:
            try:
                return bool(super().__exit__(exc_type, exc_val, exc_tb))
            finally:
                self._sync_tx_lock()
                self._enter_depth = max(0, self._enter_depth - 1)
                if self._close_on_exit and self._enter_depth == 0:
                    self.close()
        finally:
            self._op_lock.release()


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
    timeout: Optional[float] = None,
    row_factory: Any = None,
    wal: Optional[bool] = None,
    busy_timeout_ms: Optional[int] = None,
    foreign_keys: bool = False,
    close_on_exit: bool = False,
    **kwargs: Any,
) -> _stdlib_sqlite3.Connection:
    """
    全局统一 SQLite 连接工厂。

    参数:
      - database: 数据库文件路径、PathLike 或 ':memory:' / URI
      - timeout: Python 层锁等待超时秒数（默认 10.0s，保留调用方显式传入的更长超时如 30.0s）
      - row_factory: 可选行工厂（如 sqlite3.Row）
      - wal: 是否开启 WAL 模式（None 表示对非只读文件库自动开启）
      - busy_timeout_ms: SQLite C 层 busy_timeout 毫秒数（默认与 effective_timeout*1000 对齐，不覆盖长超时）
      - foreign_keys: 是否强制开启 PRAGMA foreign_keys=ON（若启用失败则关闭连接并抛异常）
      - close_on_exit: 当作为 `with connect_sqlite(...) as conn:` 使用时，退出最外层 with 块是否自动 close()
    """
    effective_timeout = max(float(timeout if timeout is not None else 10.0), 5.0)
    if busy_timeout_ms is None:
        effective_busy_ms = int(round(effective_timeout * 1000))
    elif timeout is not None:
        effective_busy_ms = max(int(busy_timeout_ms), int(round(effective_timeout * 1000)))
    else:
        effective_busy_ms = int(busy_timeout_ms)

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

    # 1. 独立设置 busy_timeout（纯内存属性，不依赖文件锁）
    if effective_busy_ms and effective_busy_ms > 0:
        try:
            conn.execute(f"PRAGMA busy_timeout={int(effective_busy_ms)}")
        except _stdlib_sqlite3.Error:
            pass

    # 2. 独立尝试设置 WAL 与 synchronous（最佳努力，若因他方持锁等原因失败绝不跳过后续约束设置）
    if enable_wal:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except _stdlib_sqlite3.Error:
            pass

    # 3. 独立强制开启并校验 foreign_keys（若调用方要求开启但失败，必须关闭连接并向上抛错）
    if foreign_keys:
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            fk_row = conn.execute("PRAGMA foreign_keys").fetchone()
            if not fk_row or int(fk_row[0]) != 1:
                raise _stdlib_sqlite3.OperationalError("无法启用 SQLite 外键约束 (PRAGMA foreign_keys=ON 未生效)")
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise

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
