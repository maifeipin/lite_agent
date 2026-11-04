"""
阶段 0/1 基线测试：契约类型单一源头、命令注册表单例与 Agent 分发行为基线
(dev/tests/architecture/test_canonical_baseline.py)

锁定：
1. 契约类型保持规范 contracts 源头 (禁止新建包装或复制定义)；
2. 技能装饰器注册与 Agent 分发使用同一个默认注册表 (services.commands.default_command_registry)；
3. 显式注入的独立 CommandRegistry 与默认全局注册表完全隔离；
4. 命令元数据（描述、权限、仪表盘展示标记）完整保留；
5. 负向反例：core/ 下已删除的 3 个纯转发模块禁止再被导入。
"""

import pytest
from types import SimpleNamespace

import lite_agent.contracts as contracts
import lite_agent.contracts.execution as contracts_execution
import lite_agent.contracts.result as contracts_result
import lite_agent.services.commands as services_commands
from lite_agent.services.commands import CommandRegistry, default_command_registry, slash_command
from lite_agent.contracts.message import IncomingMessage


def test_baseline_contract_types_canonical_source():
    """基线 1: 契约类型必须全局单一源头，规范定义位于 contracts。"""
    assert contracts_execution.ExecutionContext is contracts.ExecutionContext
    assert contracts_execution.ExecutionResult is contracts.ExecutionResult
    assert contracts_execution.SkillPolicy is contracts.SkillPolicy
    assert contracts_execution.ActorType is contracts.ActorType
    assert contracts_execution.ExecutionSource is contracts.ExecutionSource
    assert contracts_result.TaskResult is contracts.TaskResult

    # 负向反例：已删除的 3 个纯转发垫片模块禁止导入
    with pytest.raises(ModuleNotFoundError):
        __import__("lite_agent.core.execution")
    with pytest.raises(ModuleNotFoundError):
        __import__("lite_agent.core.task_result")
    with pytest.raises(ModuleNotFoundError):
        __import__("lite_agent.core.command_registry")


def test_baseline_command_registry_singleton_identity():
    """基线 2: 默认注册表单例全局唯一，services.default_command_registry 为标准源头。"""
    assert services_commands.default_command_registry is default_command_registry
    assert services_commands.CommandRegistry is CommandRegistry


def test_baseline_slash_command_registration_and_agent_dispatch_consistency():
    """基线 3: @slash_command 装饰器注册的指令必须实时反映在 default_command_registry 中，并由 Agent 正常分发。"""
    test_cmd_name = "/test_baseline_dispatch_check"
    called = []

    @slash_command(
        test_cmd_name,
        category="测试",
        description="基线测试指令",
        show_in_dashboard=True,
        guest_ok=True,
    )
    def handle_test_cmd(agent, msg, args):
        called.append((msg.text, args))
        return "baseline_ok"

    try:
        # 验证默认注册表已收录该指令与完整元数据
        entry = default_command_registry.get(test_cmd_name)
        assert entry is not None
        assert entry["category"] == "测试"
        assert entry["description"] == "基线测试指令"
        assert entry["show_in_dashboard"] is True
        assert entry["guest_ok"] is True

        # 仪表盘可见项必须包含该指令
        dashboard_items = default_command_registry.items_for_dashboard()
        assert any(item["name"] == test_cmd_name for item in dashboard_items)

        # 验证 Agent 使用默认注册表时可正常路由分发
        fake_agent = SimpleNamespace(command_registry=default_command_registry)
        fake_msg = IncomingMessage(
            channel="test",
            user_id="u1",
            chat_id="c1",
            message_id="m1",
            text=f"{test_cmd_name} arg1 arg2",
        )
        res = services_commands.dispatch(test_cmd_name, fake_agent, fake_msg, ["arg1", "arg2"])
        assert res == "baseline_ok"
        assert called == [(f"{test_cmd_name} arg1 arg2", ["arg1", "arg2"])]
    finally:
        default_command_registry.unregister(test_cmd_name)
        assert default_command_registry.get(test_cmd_name) is None


def test_baseline_explicit_command_registry_isolation():
    """基线 4: 显式注入的 CommandRegistry 实例必须与默认注册表隔离，互不干扰。"""
    isolated_reg = CommandRegistry()
    isolated_cmd = "/isolated_only_cmd"
    global_cmd = "/global_only_cmd"

    isolated_reg.register(isolated_cmd, lambda a, m, args: "isolated_val", guest_ok=True)
    default_command_registry.register(global_cmd, lambda a, m, args: "global_val", guest_ok=True)

    try:
        # 隔离注册表有 isolated_cmd，无 global_cmd
        assert isolated_reg.get(isolated_cmd) is not None
        assert isolated_reg.get(global_cmd) is None

        # 全局注册表有 global_cmd，无 isolated_cmd
        assert default_command_registry.get(global_cmd) is not None
        assert default_command_registry.get(isolated_cmd) is None

        # 分发隔离
        msg = IncomingMessage(channel="test", user_id="u1", chat_id="c1", message_id="m2", text="test")
        assert isolated_reg.dispatch(isolated_cmd, None, msg, []) == "isolated_val"
        assert isolated_reg.dispatch(global_cmd, None, msg, []) is None
        assert default_command_registry.dispatch(global_cmd, None, msg, []) == "global_val"
        assert default_command_registry.dispatch(isolated_cmd, None, msg, []) is None
    finally:
        default_command_registry.unregister(global_cmd)
