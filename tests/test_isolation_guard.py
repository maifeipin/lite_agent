"""
Unit tests for tests/support/isolation_guard.py.
Verifies negative and positive enforcement:
- Outbound external network connections are rejected (connect and connect_ex);
- Localhost connections are permitted;
- Non-temporary SQLite database writes are rejected;
- Prefix collision (/tmp-evil), PathLike objects, and URI bypasses are strictly caught;
- :memory:, mode=ro (uri=True), and tmp_path SQLite connections are permitted.
"""

import os
import socket
import sqlite3
from pathlib import Path
from unittest.mock import patch
import pytest

from tests.support.isolation_guard import (
    is_allowed_host,
    is_allowed_sqlite_target,
    install_side_effect_guards,
    uninstall_side_effect_guards,
)


def test_is_allowed_host():
    assert is_allowed_host("localhost") is True
    assert is_allowed_host("127.0.0.1") is True
    assert is_allowed_host("::1") is True
    assert is_allowed_host("api.github.com") is False
    assert is_allowed_host("8.8.8.8") is False


def test_is_allowed_sqlite_target_counterexamples(tmp_path):
    # 1. 前缀伪装 (/tmp-evil) 必须拒绝
    assert is_allowed_sqlite_target("/tmp-evil/example.db") is False

    # 2. file: 前缀但在 uri=False 时不是只读 URI 语义，必须拒绝
    assert is_allowed_sqlite_target("file:/outside/example.db?mode=ro", uri=False) is False

    # 3. PathLike 传参必须被正确识别并拒绝
    assert is_allowed_sqlite_target(Path("/outside/example.db")) is False

    # 4. 合法 uri=True mode=ro 放行
    assert is_allowed_sqlite_target("file:/outside/example.db?mode=ro", uri=True) is True

    # 5. URL 编码路径的只读 URI 放行
    assert is_allowed_sqlite_target("file:/outside/foo%23bar%3F.db?mode=ro", uri=True) is True

    # 6. :memory: 永远放行
    assert is_allowed_sqlite_target(":memory:") is True

    # 7. 受控 tmp_path 放行
    assert is_allowed_sqlite_target(str(tmp_path / "safe.db")) is True


def test_socket_connect_guard_blocks_external_host():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="connect"):
            sock.connect(("8.8.8.8", 53))
    finally:
        sock.close()


def test_socket_connect_ex_guard_blocks_external_host():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="connect_ex"):
            sock.connect_ex(("192.0.2.1", 80))
    finally:
        sock.close()


def test_sqlite_connect_guard_blocks_all_bypasses():
    # 1. 拦截 PathLike
    with pytest.raises(RuntimeError, match="测试副作用守卫拦截"):
        sqlite3.connect(Path("/outside/example.db"))

    # 2. 拦截 /tmp-evil
    with pytest.raises(RuntimeError, match="测试副作用守卫拦截"):
        sqlite3.connect("/tmp-evil/example.db")

    # 3. 拦截 uri=False 的 file: URI 伪装
    with pytest.raises(RuntimeError, match="测试副作用守卫拦截"):
        sqlite3.connect("file:/outside/example.db?mode=ro", uri=False)

    # 4. 拦截位置参数传入 uri=False 的情况
    # sqlite3.connect(database, timeout, detect_types, isolation_level, check_same_thread, factory, cached_statements, uri)
    with pytest.raises(RuntimeError, match="测试副作用守卫拦截"):
        sqlite3.connect("file:/outside/example.db?mode=ro", 5.0, 0, "DEFERRED", True, sqlite3.Connection, 128, False)


def test_sqlite_connect_guard_permits_memory_and_tmp(tmp_path):
    # :memory: allowed
    conn1 = sqlite3.connect(":memory:")
    conn1.execute("CREATE TABLE t (id INT)")
    conn1.close()

    # tmp_path allowed
    temp_db = str(tmp_path / "valid.db")
    conn2 = sqlite3.connect(temp_db)
    conn2.execute("CREATE TABLE t (id INT)")
    conn2.close()

    # mode=ro URI allowed
    uri = f"file:{temp_db}?mode=ro"
    conn3 = sqlite3.connect(uri, uri=True)
    res = conn3.execute("SELECT count(*) FROM t").fetchone()
    assert res[0] == 0
    conn3.close()


def test_production_credential_file_access_blocked():
    from core.constants import PROJECT_ROOT
    prod_env = os.path.join(PROJECT_ROOT, ".env")
    prod_cfg = os.path.join(PROJECT_ROOT, "config.json")
    with pytest.raises(PermissionError, match="测试副作用守卫拦截"):
        open(prod_env, "r")
    with pytest.raises(PermissionError, match="测试副作用守卫拦截"):
        open(prod_cfg, "r")


def test_config_loader_reads_desensitized_session_config():
    import core.config_loader as config_loader
    config_loader.bust_base_cache()
    cfg = config_loader.load_config()
    assert isinstance(cfg, dict)
    # 确认没有泄漏真实生产根配置路径
    assert os.environ.get("LITE_AGENT_DISABLE_DOTENV") == "1"
    assert os.environ.get("TESTING") == "1"

