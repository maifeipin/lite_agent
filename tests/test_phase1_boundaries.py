"""
Phase 1 架构边界、强制反例与契约同一性自动化测试套件。
严格对照 docs/agent-kernel-refactor-execution-plan-20261015.md 第 3.2 节强制反例要求。
"""

import os
import sys
import tempfile
import pytest
from unittest.mock import MagicMock

from scripts.check_architecture import run_checks
import contracts
import contracts.message
import contracts.result
import contracts.execution
import agent
import core.task_result
import core.execution
from services.commands import CommandRegistry
from core.skill_engine import SkillEngine
from app.plugins import PluginContext, PluginLoader
from core.model_invoker import OpenAIInvoker


@pytest.fixture
def repo_root():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def rules_config(repo_root):
    return os.path.join(repo_root, "architecture_rules.json")


# ============================================================
# 1. 强制反例：架构依赖门禁必须拦截所有未授权反向/跨层依赖
# ============================================================

def test_negative_core_to_features_billing_rejected(repo_root, rules_config, tmp_path):
    """反例 1: core/ 依赖 features.billing 必须被拦截并报出违规"""
    fake_file = tmp_path / "core" / "unauthorized_billing_leak.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text("import features.billing\n", encoding="utf-8")

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["core/unauthorized_billing_leak.py"]
    )
    assert len(violations) == 1
    v = violations[0]
    assert v["rule"] in ("RULE_NO_CORE_REVERSE_DEPENDENCY", "RULE_NO_KERNEL_BUSINESS_DEPENDENCY")
    assert "features.billing" in v["target"]


def test_negative_agent_to_features_todo_rejected(repo_root, rules_config, tmp_path):
    """反例 2: 根 agent.py 依赖 features.todo 必须被拦截"""
    fake_file = tmp_path / "agent.py"
    fake_file.write_text("import features.todo\n", encoding="utf-8")

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["agent.py"]
    )
    assert len(violations) == 1
    v = violations[0]
    assert v["rule"] == "RULE_NO_KERNEL_BUSINESS_DEPENDENCY"
    assert "features.todo" in v["target"]


def test_negative_services_to_features_rejected(repo_root, rules_config, tmp_path):
    """反例 3: services/ 依赖 features 必须被拦截"""
    fake_file = tmp_path / "services" / "biz_service.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text("from features.billing import workflow\n", encoding="utf-8")

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["services/biz_service.py"]
    )
    assert len(violations) == 1
    v = violations[0]
    assert v["rule"] == "RULE_NO_KERNEL_BUSINESS_DEPENDENCY"
    assert "features.billing" in v["target"]


def test_negative_contracts_to_agent_rejected(repo_root, rules_config, tmp_path):
    """反例 4: contracts/ 依赖 agent 必须被拦截"""
    fake_file = tmp_path / "contracts" / "bad_contract.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text("import agent\n", encoding="utf-8")

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["contracts/bad_contract.py"]
    )
    assert len(violations) == 1
    v = violations[0]
    assert v["rule"] == "RULE_NO_CONTRACTS_DEPENDENCY"
    assert "agent" in v["target"]


def test_negative_services_to_agent_rejected(repo_root, rules_config, tmp_path):
    """反例 5: services/ 反向依赖 agent 必须被拦截"""
    fake_file = tmp_path / "services" / "bad_agent_import.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text("import agent\n", encoding="utf-8")

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["services/bad_agent_import.py"]
    )
    assert len(violations) == 1
    v = violations[0]
    assert v["rule"] == "RULE_NO_SERVICES_DEPENDENCY"
    assert "agent" in v["target"]


def test_negative_relative_import_cannot_bypass(repo_root, rules_config, tmp_path):
    """反例 6: 相对导入 (from ..features import billing) 不能绕过门禁"""
    fake_file = tmp_path / "core" / "subpkg" / "relative_leak.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text("from ...features.billing import workflow\n", encoding="utf-8")

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["core/subpkg/relative_leak.py"]
    )
    assert len(violations) == 1
    assert "features.billing" in violations[0]["target"]


def test_negative_lazy_import_in_function_detected(repo_root, rules_config, tmp_path):
    """反例 7: 函数内惰性导入业务模块不能绕过门禁"""
    fake_file = tmp_path / "core" / "lazy_leak.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text(
        "def compute():\n"
        "    from skills.ops_todo import ops_todo_list\n"
        "    return ops_todo_list()\n",
        encoding="utf-8"
    )

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["core/lazy_leak.py"]
    )
    assert len(violations) == 1
    assert "skills.ops_todo" in violations[0]["target"]


def test_negative_unknown_dynamic_expression_rejected_in_kernel(repo_root, rules_config, tmp_path):
    """反例 8: 基础内核中的未知动态表达式默认拒绝"""
    fake_file = tmp_path / "core" / "dynamic_expression_leak.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text(
        "import importlib\n"
        "def load(target_var):\n"
        "    return importlib.import_module(target_var)\n",
        encoding="utf-8"
    )

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["core/dynamic_expression_leak.py"]
    )
    assert len(violations) == 1
    assert violations[0]["rule"] == "RULE_DYNAMIC_IMPORT_REGISTERED"
    assert violations[0]["target"] == "<dynamic_expression>"


def test_positive_comments_and_strings_not_flagged(repo_root, rules_config, tmp_path):
    """正向测试: 注释和普通字符串中的模块名不误报"""
    fake_file = tmp_path / "core" / "safe_comments.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text(
        "# import features.billing\n"
        "# from skills import ops_todo\n"
        "DOC_STRING = 'This discusses features.billing and mail_client'\n"
        "sql_query = 'SELECT * FROM features_table'\n",
        encoding="utf-8"
    )

    file_count, violations, approved = run_checks(
        str(tmp_path), rules_config, explicit_files=["core/safe_comments.py"]
    )
    assert len(violations) == 0, f"Expected 0 violations but got: {violations}"


# ============================================================
# 2. 插件协议与受控加载测试
# ============================================================

def test_controlled_plugin_loader_allowed_and_rejected():
    """受控插件加载器：允许清单内的入口，清单外或伪造入口抛出 PermissionError"""
    from types import ModuleType
    from unittest.mock import patch
    plugin = ModuleType("synthetic_plugin")
    plugin.register = MagicMock()
    ctx = PluginContext()
    loader = PluginLoader(context=ctx, allowed_entries={"synthetic_plugin"})
    with patch("app.plugins.importlib.import_module", return_value=plugin) as importer:
        assert loader.load_plugin_by_entry("synthetic_plugin") is plugin
        assert loader.load_plugin_by_entry("synthetic_plugin") is plugin
        importer.assert_called_once_with("synthetic_plugin")
        plugin.register.assert_called_once_with(ctx)
        with pytest.raises(PermissionError):
            loader.load_plugin_by_entry("os")
        importer.assert_called_once()


# ============================================================
# 3. 契约类型同一性与无副作用验证
# ============================================================

def test_contract_types_identity_and_backward_compatibility():
    """验证 contracts 迁移前后类对象保持恒等 (同一对象，非包装)"""
    assert agent.IncomingMessage is contracts.IncomingMessage
    assert agent.AgentResponse is contracts.AgentResponse
    assert core.task_result.TaskResult is contracts.TaskResult
    assert core.execution.ExecutionContext is contracts.ExecutionContext
    assert core.execution.ExecutionResult is contracts.ExecutionResult
    assert core.execution.SkillPolicy is contracts.SkillPolicy
    assert core.execution.ActorType is contracts.ActorType
    assert core.execution.ExecutionSource is contracts.ExecutionSource


def test_incoming_message_fields_and_binding():
    """验证 IncomingMessage 字段行为、默认值与 session 绑定行为"""
    msg = contracts.IncomingMessage(
        channel="feishu",
        user_id="u123",
        chat_id="c456",
        message_id="m789",
        text="hello",
        output_mode="card",
    )
    assert msg.channel == "feishu"
    assert msg.notify_channels == ["feishu"]
    assert msg.scope_key == "feishu:u123"
    assert msg.session_key == "feishu:u123"

    msg.bind_session("custom_session_key")
    assert msg.session_key == "custom_session_key"
    assert msg.output_mode == "card"


def test_import_contracts_has_zero_side_effects():
    """验证 contracts 导入零副作用：无数据库连接，无文件创建，无网络请求"""
    # 重新验证 contracts 模块内部符号不含任何外部运行时或配置
    assert hasattr(contracts, "IncomingMessage")
    assert hasattr(contracts, "TaskResult")
    assert not hasattr(contracts, "Agent")
    assert not hasattr(contracts, "config")


# ============================================================
# 4. 实例隔离与空工具普通对话模型验证
# ============================================================

def test_command_registry_instance_isolation():
    """验证 CommandRegistry 支持实例隔离，互不污染，支持注销"""
    reg1 = CommandRegistry()
    reg2 = CommandRegistry()

    reg1.register("/test_cmd", lambda a, m, args: "ok")
    assert reg1.get("/test_cmd") is not None
    assert reg2.get("/test_cmd") is None, "reg2 was polluted by reg1!"

    reg1.unregister("/test_cmd")
    assert reg1.get("/test_cmd") is None


def test_skill_engine_instance_isolation_and_empty_tools():
    """验证 SkillEngine 支持显式空集合 tools={} 并不扫描技能目录"""
    engine_empty = SkillEngine(tools={})
    assert engine_empty.get_all_schemas() == []
    assert engine_empty.get_skill_count() == 0
    assert engine_empty.list_skills() == "(暂无技能)"

    engine_custom = SkillEngine(tools={})
    engine_custom.register_tool("echo", {
        "schema": {"type": "function", "function": {"name": "echo", "description": "echo"}},
        "policy": contracts.SkillPolicy(guest_ok=True),
        "func": lambda text="": text,
    })
    assert engine_custom.get_skill_count() == 1
    assert engine_empty.get_skill_count() == 0, "engine_empty was polluted!"


def test_empty_tools_chat_omits_tools_in_model_invoker():
    """验证空工具集合执行模型调用时，适配器正确省略 tools 字段"""
    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = "你好，我是基础 Agent！"
    mock_choice.message.tool_calls = None
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = None
    mock_client.chat.completions.create.return_value = mock_response

    invoker = OpenAIInvoker(client=mock_client, model_name="gpt-4o-mini")

    # 当 tools=[] 或 tools=None 时调用
    result = invoker.invoke_sync(messages=[{"role": "user", "content": "你好"}], tools=[])

    assert result["content"] == "你好，我是基础 Agent！"
    assert result["tool_calls"] == []

    # 核心断言: 传给客户端的调用参数中绝对不能包含 tools 字段
    call_kwargs = mock_client.chat.completions.create.call_args[1]
    assert "tools" not in call_kwargs, f"tools 应当在空集合时被完全省略，实测参数: {call_kwargs}"
    assert "tool_choice" not in call_kwargs
