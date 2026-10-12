"""
Mail Statement Parser 路径与数据库只读连接助手。
统一 Agent 端对外部 parser 脚本目录与数据库路径的解析，
避免不同技能分别采用不同的相对路径或直接创建/写入默认数据库。
"""

import os
import sqlite3
from typing import Optional


def get_parser_script_dir() -> str:
    """
    解析 mail-statement-parser 脚本目录绝对路径。
    优先级：环境变量 MAIL_STATEMENT_PARSER_DIR > config.json > 本地相对路径候选 > 生产默认路径。
    """
    env_dir = os.environ.get("MAIL_STATEMENT_PARSER_DIR")
    if env_dir:
        return os.path.abspath(os.path.expanduser(env_dir))

    try:
        from core import config_loader
        cfg = config_loader.load_config() or {}
        configured_dir = cfg.get("billing", {}).get("script_dir")
        if configured_dir:
            return os.path.abspath(os.path.expanduser(configured_dir))
    except Exception:
        pass

    # 尝试本地开发相对路径（仅在未显式配置时）
    for rel_path in [
        ("..", "..", "dev", "mail-statement-parser"),
        ("..", "..", "mail-statement-parser"),
        ("..", "..", "..", "mail-statement-parser"),
        ("..", "..", "..", "dev", "mail-statement-parser"),
    ]:
        candidate = os.path.abspath(
            os.path.join(os.path.dirname(__file__), *rel_path)
        )
        if os.path.isdir(candidate):
            return candidate

    return "/home/liteagent/mail-statement-parser"


def get_statement_db_path(custom_db_path: Optional[str] = None, script_dir: Optional[str] = None) -> str:
    """
    解析 statements.db 绝对路径。
    优先级：显式参数 > 环境变量 MAIL_STATEMENT_DB_PATH > config.json > script_dir 下 statements.db。
    """
    if custom_db_path:
        return os.path.abspath(os.path.expanduser(custom_db_path))

    env_db = os.environ.get("MAIL_STATEMENT_DB_PATH")
    if env_db:
        return os.path.abspath(os.path.expanduser(env_db))

    base_dir = os.path.abspath(os.path.expanduser(script_dir)) if script_dir else get_parser_script_dir()
    try:
        from core import config_loader
        cfg = config_loader.load_config() or {}
        configured_db = cfg.get("billing", {}).get("statement_db_path")
        if configured_db:
            if os.path.isabs(configured_db):
                return os.path.abspath(os.path.expanduser(configured_db))
            # 相对路径相对 script_dir
            return os.path.abspath(os.path.join(base_dir, os.path.expanduser(configured_db)))
    except Exception:
        pass

    return os.path.abspath(os.path.join(base_dir, "statements.db"))


def connect_readonly_db(db_path: Optional[str] = None) -> Optional[sqlite3.Connection]:
    """
    建立 SQLite 只读连接 (URI mode=ro)。
    如果数据库文件不存在，返回 None，绝不自动新建数据库。
    """
    target = get_statement_db_path(db_path)
    if not os.path.exists(target):
        return None

    import urllib.parse
    # SQLite URI 只读连接模式：规范编码防止文件名含 ? 或 # 等破坏 URI
    quoted_target = urllib.parse.quote(os.path.abspath(target), safe="/")
    uri = f"file:{quoted_target}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn
