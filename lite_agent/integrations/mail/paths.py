"""
Mail Statement Parser 路径与数据库只读连接助手。
统一 Agent 端对外部 parser 脚本目录与数据库路径的解析，
避免不同技能分别采用不同的相对路径或直接创建/写入默认数据库。
"""

import logging
import os
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)


def get_parser_script_dir() -> str:
    """
    解析 mail-statement-parser 脚本目录绝对路径。
    优先级：环境变量 MAIL_STATEMENT_PARSER_DIR > config.json > 本地相对路径候选 > 生产默认路径。
    """
    env_dir = os.environ.get("MAIL_STATEMENT_PARSER_DIR")
    if env_dir:
        return os.path.abspath(os.path.expanduser(env_dir))

    try:
        from lite_agent.core import config_loader
        cfg = config_loader.load_config() or {}
        configured_dir = cfg.get("billing", {}).get("script_dir")
        if configured_dir:
            return os.path.abspath(os.path.expanduser(configured_dir))
    except Exception:
        pass

    # 尝试本地开发相对路径（仅在未显式配置时）
    from lite_agent.core.constants import PROJECT_ROOT
    for candidate in [
        os.path.join(PROJECT_ROOT, "dev", "mail-statement-parser"),
        os.path.join(PROJECT_ROOT, "mail-statement-parser"),
        os.path.join(os.path.dirname(PROJECT_ROOT), "mail-statement-parser"),
        os.path.join(os.path.dirname(PROJECT_ROOT), "dev", "mail-statement-parser"),
    ]:
        cand_abs = os.path.abspath(candidate)
        if os.path.isdir(cand_abs):
            return cand_abs

    return os.path.expanduser("~/mail-statement-parser")


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
        from lite_agent.core import config_loader
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
    from lite_agent.core.sqlite_utils import connect_sqlite
    # SQLite URI 只读连接模式：规范编码防止文件名含 ? 或 # 等破坏 URI
    quoted_target = urllib.parse.quote(os.path.abspath(target), safe="/")
    uri = f"file:{quoted_target}?mode=ro"
    return connect_sqlite(uri, uri=True, row_factory=sqlite3.Row, wal=False)


def _validate_explicit_executable(raw_path: str, source_desc: str) -> str:
    """严格校验显式指定的解释器路径：先展开、规范化，并检查文件存在与执行权限，失败直接报错拒绝静默换用。"""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"{source_desc} 解释器路径不能为空")

    expanded = os.path.abspath(os.path.expanduser(raw_path.strip()))
    if not os.path.exists(expanded):
        raise FileNotFoundError(f"{source_desc} 指定的 Python 解释器不存在: {expanded}")
    if not os.path.isfile(expanded):
        raise ValueError(f"{source_desc} 指定的路径不是有效文件: {expanded}")
    if not os.access(expanded, os.X_OK):
        raise PermissionError(f"{source_desc} 指定的 Python 解释器缺少可执行权限 (X_OK): {expanded}")
    return expanded


_logged_parser_python: Optional[str] = None


def _record_and_log_parser_python(chosen: str, reason: str) -> str:
    global _logged_parser_python
    if _logged_parser_python != chosen:
        _logged_parser_python = chosen
        logger.info("选中 mail-statement-parser Python 解释器: %s (来源: %s)", chosen, reason)
    return chosen


def get_parser_python_executable(script_dir: Optional[str] = None) -> str:
    """
    解析 mail-statement-parser 专用 Python 解释器绝对路径。
    规则：
    1. 显式配置优先，严格校验存在性与执行权限，失败直接报错，绝不静默降级：
       - 环境变量 MAIL_STATEMENT_PARSER_PYTHON
       - config.json 中 billing.python_executable
    2. 未显式配置时，依次自动发现与受控回退：
       - script_dir/.venv/bin/python (若存在独立虚拟环境且可执行)
       - 系统已知稳定解释器 (/usr/bin/python3 且可执行)
       - 当前进程宿主解释器 sys.executable
    """
    # 1. 显式环境变量配置
    env_py = os.environ.get("MAIL_STATEMENT_PARSER_PYTHON")
    if env_py is not None and env_py.strip():
        chosen = _validate_explicit_executable(env_py, "环境变量 MAIL_STATEMENT_PARSER_PYTHON")
        return _record_and_log_parser_python(chosen, "环境变量 MAIL_STATEMENT_PARSER_PYTHON")

    # 2. 显式配置文件配置
    base_dir = os.path.abspath(os.path.expanduser(script_dir)) if script_dir else get_parser_script_dir()
    try:
        from lite_agent.core import config_loader
        cfg = config_loader.load_config() or {}
        configured_py = cfg.get("billing", {}).get("python_executable")
        if configured_py is not None and str(configured_py).strip():
            chosen = _validate_explicit_executable(str(configured_py), "配置文件 billing.python_executable")
            return _record_and_log_parser_python(chosen, "配置文件 billing.python_executable")
    except (FileNotFoundError, ValueError, PermissionError):
        raise
    except Exception:
        pass

    # 3. 自动发现：parser 项目独立虚拟环境
    venv_py = os.path.join(base_dir, ".venv", "bin", "python")
    if os.path.isfile(venv_py) and os.access(venv_py, os.X_OK):
        chosen = os.path.abspath(venv_py)
        return _record_and_log_parser_python(chosen, f"独立虚拟环境 ({venv_py})")

    # 4. 受控回退：宿主环境默认系统解释器
    if os.path.isfile("/usr/bin/python3") and os.access("/usr/bin/python3", os.X_OK):
        chosen = "/usr/bin/python3"
        return _record_and_log_parser_python(chosen, "系统默认解释器 (/usr/bin/python3)")

    # 5. 终极兜底：当前进程解释器
    import sys
    chosen = sys.executable
    return _record_and_log_parser_python(chosen, "宿主进程当前解释器 (sys.executable)")


