"""
Phase 0 行为锁定测试 — 共享 fixtures 与全生命周期副作用隔离。

隔离策略：
1. 收集期即刻安装副作用拦截器 (install_side_effect_guards)，覆盖测试模块导入与收集阶段；
2. 每次测试运行建立独立唯一的临时会话根目录 (_TEST_SESSION_DIR)，DB、数据根、报告、下载、session、memory 全置于其内；
3. 保存并重写外部环境变量，测试结束后在 pytest_sessionfinish 中完全恢复并清理临时目录；
4. 隔离 _skill_registry 与测试技能，禁止测试触碰生产配置与数据。
"""

import os
import sys
import shutil
import tempfile
import pytest

# 1. 顶层立即安装副作用守卫（早于任何测试模块收集与导入）
from dev.tests.support.isolation_guard import (
    install_side_effect_guards,
    uninstall_side_effect_guards,
    set_active_test_root,
)
install_side_effect_guards()

# 装配测试期全量业务领域映射到 core.request_selector.DOMAIN_MAP
try:
    from dev.tests.support.manifest_domains import get_all_manifest_domains
    from lite_agent.core.request_selector import DOMAIN_MAP
    DOMAIN_MAP.update(get_all_manifest_domains())
except Exception:
    pass

# 2. 建立测试会话独占临时根目录
_TEST_SESSION_DIR = tempfile.mkdtemp(prefix="lite_agent_test_session_")
set_active_test_root(_TEST_SESSION_DIR)

_DATA_ROOT = os.path.join(_TEST_SESSION_DIR, "data_root")
os.makedirs(_DATA_ROOT, exist_ok=True)
os.makedirs(os.path.join(_DATA_ROOT, "validation-reports"), exist_ok=True)
os.makedirs(os.path.join(_DATA_ROOT, "email-downloads"), exist_ok=True)

# 建立会话脱敏测试配置（基于 config.example.json，严禁测试读取真实 config.json/.env）
_TEST_CONFIG_JSON = os.path.join(_TEST_SESSION_DIR, "config.json")
_TEST_CONF_D = os.path.join(_TEST_SESSION_DIR, "conf.d")
os.makedirs(_TEST_CONF_D, exist_ok=True)
_example_cfg = os.path.realpath(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config/examples/agent.json")))
if os.path.isfile(_example_cfg):
    import json
    with open(_example_cfg, "r", encoding="utf-8") as _f:
        _cfg_data = json.load(_f)
    _sb_paths = _cfg_data.setdefault("security", {}).setdefault("sandbox_paths", [])
    for _p in [_TEST_SESSION_DIR, tempfile.gettempdir(), "/parser", "/dummy_parser"]:
        if _p not in _sb_paths:
            _sb_paths.append(_p)
    _parser_candidates = [
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mail-statement-parser")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "dev", "mail-statement-parser")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "mail-statement-parser")),
    ]
    for _cand in _parser_candidates:
        if os.path.isdir(_cand):
            if _cand not in _sb_paths:
                _sb_paths.append(_cand)
            if "MAIL_STATEMENT_PARSER_DIR" not in os.environ:
                os.environ["MAIL_STATEMENT_PARSER_DIR"] = _cand
            break
    _env_parser = os.environ.get("MAIL_STATEMENT_PARSER_DIR")
    if _env_parser and _env_parser not in _sb_paths:
        _sb_paths.append(_env_parser)
    with open(_TEST_CONFIG_JSON, "w", encoding="utf-8") as _f:
        json.dump(_cfg_data, _f, indent=2, ensure_ascii=False)
else:
    with open(_TEST_CONFIG_JSON, "w", encoding="utf-8") as _f:
        _f.write("{}")

# 3. 保存并覆写关键环境变量（强制覆盖现有外部 env，阻断外部指向真实数据与配置）
_ENV_KEYS = [
    "TESTING",
    "MAIL_STATEMENT_PARSER_DIR",
    "MAIL_STATEMENT_DATA_ROOT",
    "MAIL_STATEMENT_DB_PATH",
    "MAIL_STATEMENT_REPORT_DIR",
    "MAIL_STATEMENT_DOWNLOAD_DIR",
    "LITE_AGENT_SESSION_DB_PATH",
    "USER_SESSION_DB",
    "LITE_AGENT_MEMORY_DB_PATH",
    "LITE_AGENT_LEDGER_DB_PATH",
    "LITE_AGENT_EDGE_DB_PATH",
    "LITE_AGENT_CONFIG_ROOT",
    "LITE_AGENT_CONFIG_FILE",
    "LITE_AGENT_SETTINGS_DB_PATH",
    "LITE_AGENT_DISABLE_DOTENV",
]
_SAVED_ENV = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}

os.environ["TESTING"] = "1"
os.environ["LITE_AGENT_DISABLE_DOTENV"] = "1"
os.environ["LITE_AGENT_CONFIG_ROOT"] = _TEST_SESSION_DIR
os.environ["LITE_AGENT_CONFIG_FILE"] = _TEST_CONFIG_JSON
os.environ["LITE_AGENT_SETTINGS_DB_PATH"] = os.path.join(_TEST_SESSION_DIR, "settings.db")
os.environ["MAIL_STATEMENT_DATA_ROOT"] = _DATA_ROOT
os.environ["MAIL_STATEMENT_DB_PATH"] = os.path.join(_DATA_ROOT, "statements.db")
os.environ["MAIL_STATEMENT_REPORT_DIR"] = os.path.join(_DATA_ROOT, "validation-reports")
os.environ["MAIL_STATEMENT_DOWNLOAD_DIR"] = os.path.join(_DATA_ROOT, "email-downloads")
os.environ["LITE_AGENT_SESSION_DB_PATH"] = os.path.join(_TEST_SESSION_DIR, "sessions.db")
os.environ["USER_SESSION_DB"] = os.path.join(_TEST_SESSION_DIR, "user_sessions.db")
os.environ["LITE_AGENT_MEMORY_DB_PATH"] = os.path.join(_TEST_SESSION_DIR, "memory.db")
os.environ["LITE_AGENT_LEDGER_DB_PATH"] = os.path.join(_TEST_SESSION_DIR, "execution_ledger.db")
os.environ["LITE_AGENT_EDGE_DB_PATH"] = os.path.join(_TEST_SESSION_DIR, "edge_tasks.db")


def pytest_sessionfinish(session, exitstatus):
    """测试会话结束时，恢复环境变量并彻底清理临时会话根目录。"""
    uninstall_side_effect_guards()
    set_active_test_root(None)

    # 完整恢复环境变量（含被覆盖或新增的环境变量）
    for k in _ENV_KEYS:
        if k in _SAVED_ENV:
            os.environ[k] = _SAVED_ENV[k]
        else:
            os.environ.pop(k, None)

    # 清理会话临时目录
    if os.path.exists(_TEST_SESSION_DIR):
        shutil.rmtree(_TEST_SESSION_DIR, ignore_errors=True)


from lite_agent.core.skill_engine import _skill_registry, skill, SkillEngine


@pytest.fixture(autouse=True)
def _isolate_skill_registry():
    """每个测试前后保存/恢复全局 _skill_registry，防止测试间污染。"""
    saved = dict(_skill_registry)
    _skill_registry.clear()
    yield
    _skill_registry.clear()
    _skill_registry.update(saved)


@pytest.fixture
def engine():
    """返回一个干净的 SkillEngine 实例（不扫描 skills/ 目录）。"""
    return SkillEngine(skills_dir="/tmp/__nonexistent_skills__")


@pytest.fixture
def engine_with_test_skills():
    """返回一个 SkillEngine，注册了四个纯测试技能。"""
    _skill_registry.clear()

    @skill(
        name="test_echo",
        description="回显输入",
        params={"text": {"type": "string", "description": "要回显的文本"}},
        side_effect=False,
        dry_run_handler=lambda text: f"echo: {text}",
    )
    def test_echo(text: str) -> str:
        return f"echo: {text}"

    @skill(
        name="test_raise",
        description="总是抛出异常",
        params={"msg": {"type": "string", "description": "异常消息"}},
        side_effect=None,
    )
    def test_raise(msg: str = "boom") -> str:
        raise RuntimeError(msg)

    @skill(
        name="test_guest_ok",
        description="访客可用的技能",
        params={},
        guest_ok=True,
        side_effect=False,
        dry_run_handler=lambda: "guest allowed",
    )
    def test_guest_ok() -> str:
        return "guest allowed"

    @skill(
        name="test_write",
        description="写入操作（有副作用，支持 dry-run）",
        params={"data": {"type": "string", "description": "要写入的数据"}},
        side_effect=True,
        dry_run_handler=lambda data: f"[dry-run] would write: {data}",
    )
    def test_write(data: str) -> str:
        return f"wrote: {data}"

    return SkillEngine(skills_dir="/tmp/__nonexistent_skills__")
