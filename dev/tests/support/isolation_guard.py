"""
测试副作用守卫 (Test Side-Effect Guard)
- 阻止向生产/真实数据根写入 SQLite 数据库（只允许 :memory:, mode=ro 或受控测试临时根）
- 阻止测试期间发起非 localhost 的真实外网网络连接 (覆盖 connect 与 connect_ex)
- 严格遵循 commonpath 根边界检查，防范 /tmp-evil 前缀伪装、PathLike 绕过与 URI 伪装
"""

import os
import socket
import sqlite3
import tempfile
import urllib.parse
from typing import Optional, Set
from urllib.parse import parse_qs, urlparse

_ALLOWED_HOSTS: Set[str] = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
}

_ACTIVE_TEST_ROOT: Optional[str] = None

_orig_socket_connect = socket.socket.connect
_orig_socket_connect_ex = socket.socket.connect_ex
_orig_sqlite_connect = sqlite3.connect


def set_active_test_root(path: Optional[str]):
    """设置当前测试会话绑定的独占临时根目录。"""
    global _ACTIVE_TEST_ROOT
    _ACTIVE_TEST_ROOT = os.path.realpath(os.path.abspath(path)) if path else None


def get_active_test_root() -> Optional[str]:
    return _ACTIVE_TEST_ROOT


def is_allowed_host(host: str) -> bool:
    if not host or not isinstance(host, str):
        return True
    host_lower = host.lower()
    return host_lower in _ALLOWED_HOSTS or host_lower.endswith(".localhost")


def is_allowed_sqlite_target(database, uri: bool = False) -> bool:
    """
    严格检查 SQLite 连接目标是否安全受控。
    - :memory: 永远允许
    - uri=True 时且指定了 mode=ro 只读模式，允许读取任意存在文件
    - uri=False 时即使用户路径包含 file: 也作为普通字面文件名，严禁只读放行
    - 普通写目标必须严格位于受控测试根目录下 (通过 commonpath 边界检查，阻断前缀绕过)
    """
    try:
        db_target = os.fspath(database)
    except Exception:
        return False

    if not isinstance(db_target, str):
        return False

    if db_target == ":memory:":
        return True

    # 仅在调用方显式声明 uri=True 时，才按 SQLite URI 语义解析
    if uri:
        if db_target.startswith("file:"):
            parsed = urlparse(db_target)
            qs = parse_qs(parsed.query)
            # 如果是只读模式 mode=ro，安全允许
            if qs.get("mode") == ["ro"]:
                return True
            path = urllib.parse.unquote(parsed.path)
        else:
            path = db_target
    else:
        # uri=False 时，无论文件名如何，均为本地写目标，不按 URI 解释
        path = db_target

    if not path:
        return True

    norm_path = os.path.realpath(os.path.abspath(path))

    # 构造合法的临时目录候选根
    allowed_roots = []
    if _ACTIVE_TEST_ROOT:
        allowed_roots.append(_ACTIVE_TEST_ROOT)

    allowed_roots.append(os.path.realpath(tempfile.gettempdir()))
    for sys_tmp in ("/tmp", "/private/tmp", "/var/folders"):
        if os.path.exists(sys_tmp):
            allowed_roots.append(os.path.realpath(sys_tmp))

    # 使用 commonpath 判定是否真正位于合法根之下（严禁 startswith 前缀字符串匹配）
    for root in allowed_roots:
        try:
            if os.path.commonpath([root, norm_path]) == root:
                return True
        except ValueError:
            continue

    return False


import builtins

_orig_open = builtins.open


def install_side_effect_guards():
    """安装测试运行时副作用拦截器。"""

    def guarded_open(file, *args, **kwargs):
        try:
            fspath = os.fspath(file)
            if isinstance(fspath, str):
                norm = os.path.realpath(os.path.abspath(fspath))
                base_agent = os.path.realpath(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
                prod_env = os.path.join(base_agent, ".env")
                prod_cfg = os.path.join(base_agent, "config.json")
                prod_confd = os.path.join(base_agent, "conf.d")
                if norm == prod_env or norm == prod_cfg or (os.path.exists(prod_confd) and os.path.commonpath([prod_confd, norm]) == prod_confd):
                    raise PermissionError(
                        f"测试副作用守卫拦截: 禁止在测试中读取生产配置/凭据文件 '{fspath}'！测试必须使用脱敏 fixture 或会话临时配置。"
                    )
        except (ValueError, TypeError, AttributeError):
            pass
        return _orig_open(file, *args, **kwargs)

    def guarded_socket_connect(self, address):
        if isinstance(address, tuple) and len(address) >= 2:
            host, port = address[0], address[1]
            if not is_allowed_host(host):
                raise RuntimeError(
                    f"测试副作用守卫拦截 (connect): 禁止在测试中连接真实外网目标 {host}:{port}！请使用 mock 或 localhost。"
                )
        return _orig_socket_connect(self, address)

    def guarded_socket_connect_ex(self, address):
        if isinstance(address, tuple) and len(address) >= 2:
            host, port = address[0], address[1]
            if not is_allowed_host(host):
                raise RuntimeError(
                    f"测试副作用守卫拦截 (connect_ex): 禁止在测试中连接真实外网目标 {host}:{port}！请使用 mock 或 localhost。"
                )
        return _orig_socket_connect_ex(self, address)

    def guarded_sqlite_connect(database, *args, **kwargs):
        is_uri = kwargs.get("uri", False)
        if not is_uri and len(args) >= 7:
            is_uri = bool(args[6])

        if not is_allowed_sqlite_target(database, uri=is_uri):
            raise RuntimeError(
                f"测试副作用守卫拦截: 禁止在测试中向非受控路径写入 SQLite 数据库 '{database}'！只允许 :memory:, mode=ro (uri=True) 或测试临时根。"
            )
        return _orig_sqlite_connect(database, *args, **kwargs)

    builtins.open = guarded_open
    socket.socket.connect = guarded_socket_connect
    socket.socket.connect_ex = guarded_socket_connect_ex
    sqlite3.connect = guarded_sqlite_connect


def uninstall_side_effect_guards():
    """卸载测试拦截器。"""
    builtins.open = _orig_open
    socket.socket.connect = _orig_socket_connect
    socket.socket.connect_ex = _orig_socket_connect_ex
    sqlite3.connect = _orig_sqlite_connect
