"""
阶段 0/1 基线测试：契约类型单一源头、命令注册表单例与 Agent 分发行为基线
(dev/tests/architecture/test_canonical_baseline.py)

锁定：
1. 契约类型保持规范 contracts 源头 (禁止新建包装或复制定义)；
2. 技能装饰器注册与 Agent 分发使用同一个默认注册表 (services.commands.default_command_registry)；
3. 显式注入的独立 CommandRegistry 与默认全局注册表完全隔离；
4. 命令元数据（描述、权限、仪表盘展示标记）完整保留；
5. 负向反例：core/ 下已删除的 3 个纯转发模块禁止再被导入；
6. 真实 Agent 分发入口 (_handle_builtin, _handle_double_colon) 行为基线：
   - 默认注册表指令在两种分发路径均可执行；
   - 注入独立注册表后，仅执行该实例的指令；
   - 全局独有指令不会被执行 (不回退全局注册表)；
   - guest_ok=False 权限检查在两种路径均严格生效。
"""

import pytest
from unittest.mock import Mock

import lite_agent.contracts as contracts
import lite_agent.contracts.execution as contracts_execution
import lite_agent.contracts.result as contracts_result
import lite_agent.services.commands as services_commands
from lite_agent.services.commands import CommandRegistry, default_command_registry, slash_command
from lite_agent.contracts.message import AgentResponse, IncomingMessage
from lite_agent.agent import Agent


def _make_agent(command_registry=None):
    agent = Agent.__new__(Agent)
    agent._config = {}
    agent.command_registry = command_registry
    agent._run_ai_loop = Mock(return_value=AgentResponse("ai loop fallback"))
    return agent


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
    """基线 3: @slash_command 装饰器注册的指令必须实时反映在 default_command_registry 中，
    且通过真实 Agent._handle_builtin() 与 Agent._handle_double_colon() 入口正常分发。"""
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

        # 验证真实 Agent 使用默认注册表时，经由 _handle_builtin 与 _handle_double_colon 分发
        default_agent = _make_agent(command_registry=default_command_registry)

        # 1. / 路由 (_handle_builtin)
        msg_builtin = IncomingMessage(
            channel="test",
            user_id="u1",
            chat_id="c1",
            message_id="m1",
            text=f"{test_cmd_name} arg1 arg2",
            is_guest=False,
        )
        res_builtin = default_agent._handle_builtin(msg_builtin)
        assert res_builtin.text == "baseline_ok"
        assert res_builtin.title == "执行结果"

        # 2. :: 路由 (_handle_double_colon)
        msg_double_colon = IncomingMessage(
            channel="test",
            user_id="u1",
            chat_id="c1",
            message_id="m2",
            text=f"::{test_cmd_name.lstrip('/')} arg1 arg2",
            is_guest=False,
        )
        res_double_colon = default_agent._handle_double_colon(msg_double_colon)
        assert res_double_colon.text == "baseline_ok"
        assert res_double_colon.title == "执行结果"

        # 3. 验证未显式注入注册表（使用默认单例）的 Agent 也能正常分发
        fallback_agent = _make_agent(command_registry=None)
        res_fallback = fallback_agent._handle_builtin(msg_builtin)
        assert res_fallback.text == "baseline_ok"

        assert called == [
            (f"{test_cmd_name} arg1 arg2", ["arg1", "arg2"]),
            (f"::{test_cmd_name.lstrip('/')} arg1 arg2", ["arg1", "arg2"]),
            (f"{test_cmd_name} arg1 arg2", ["arg1", "arg2"]),
        ]
    finally:
        default_command_registry.unregister(test_cmd_name)
        assert default_command_registry.get(test_cmd_name) is None


def test_baseline_explicit_command_registry_isolation():
    """基线 4: 显式注入独立 CommandRegistry 实例时，Agent 严格仅执行该实例的指令，
    不回退到全局默认注册表，且 guest_ok=False 的权限检查在 / 与 :: 路径均生效。"""
    isolated_reg = CommandRegistry()
    isolated_cmd = "/isolated_only_cmd"
    global_cmd = "/global_only_cmd"
    admin_cmd = "/isolated_admin_cmd"

    isolated_called = []
    global_called = []
    admin_called = []

    def handle_isolated(agent, msg, args):
        isolated_called.append((msg.text, args))
        return "isolated_val"

    def handle_global(agent, msg, args):
        global_called.append((msg.text, args))
        return "global_val"

    def handle_admin(agent, msg, args):
        admin_called.append((msg.text, args))
        return "admin_val"

    isolated_reg.register(isolated_cmd, handle_isolated, guest_ok=True)
    isolated_reg.register(admin_cmd, handle_admin, guest_ok=False)
    default_command_registry.register(global_cmd, handle_global, guest_ok=True)

    try:
        # 1. 注册表元数据隔离确认
        assert isolated_reg.get(isolated_cmd) is not None
        assert isolated_reg.get(admin_cmd) is not None
        assert isolated_reg.get(global_cmd) is None

        assert default_command_registry.get(global_cmd) is not None
        assert default_command_registry.get(isolated_cmd) is None
        assert default_command_registry.get(admin_cmd) is None

        # 2. 构造注入了独立注册表的 Agent
        isolated_agent = _make_agent(command_registry=isolated_reg)

        # 属性 1: 注入独立注册表后，仅执行该实例的指令 (/ 与 :: 均通过)
        msg_iso_slash = IncomingMessage("test", "u1", "c1", "m1", f"{isolated_cmd} a1", is_guest=False)
        resp_iso_slash = isolated_agent._handle_builtin(msg_iso_slash)
        assert resp_iso_slash.text == "isolated_val"

        msg_iso_dc = IncomingMessage("test", "u1", "c1", "m2", f"::{isolated_cmd.lstrip('/')} a2", is_guest=False)
        resp_iso_dc = isolated_agent._handle_double_colon(msg_iso_dc)
        assert resp_iso_dc.text == "isolated_val"
        assert len(isolated_called) == 2

        # 属性 2: 全局独有指令绝不被执行（无全局注册表回退）
        # 验证 / 指令回退到 AI loop 而非执行全局指令
        msg_global_slash = IncomingMessage("test", "u1", "c1", "m3", f"{global_cmd} g1", is_guest=False)
        resp_global_slash = isolated_agent._handle_builtin(msg_global_slash)
        assert resp_global_slash.text == "ai loop fallback"

        # 验证 :: 指令返回未知指令报错而非执行全局指令
        msg_global_dc = IncomingMessage("test", "u1", "c1", "m4", f"::{global_cmd.lstrip('/')} g2", is_guest=False)
        resp_global_dc = isolated_agent._handle_double_colon(msg_global_dc)
        assert f"未知指令 `::{global_cmd.lstrip('/')}`" in resp_global_dc.text
        assert resp_global_dc.title == "⚠️"

        # 全局指令 handler 绝对没有被调用
        assert len(global_called) == 0

        # 属性 3: guest_ok=False 权限检查在 / 与 :: 路径严格拦截
        # 访客身份 (is_guest=True) 尝试调用管理员指令：被权限检查拦截
        msg_admin_guest_slash = IncomingMessage("test", "u1", "c1", "m5", f"{admin_cmd}", is_guest=True)
        resp_admin_guest_slash = isolated_agent._handle_builtin(msg_admin_guest_slash)
        assert "权限不足" in resp_admin_guest_slash.text
        assert resp_admin_guest_slash.title == "⚠️ 权限不足"

        msg_admin_guest_dc = IncomingMessage("test", "u1", "c1", "m6", f"::{admin_cmd.lstrip('/')}", is_guest=True)
        resp_admin_guest_dc = isolated_agent._handle_double_colon(msg_admin_guest_dc)
        assert "权限不足" in resp_admin_guest_dc.text
        assert resp_admin_guest_dc.title == "⚠️ 权限不足"
        assert len(admin_called) == 0

        # 非访客身份 (is_guest=False) 正常执行
        msg_admin_user_slash = IncomingMessage("test", "u1", "c1", "m7", f"{admin_cmd} ok1", is_guest=False)
        resp_admin_user_slash = isolated_agent._handle_builtin(msg_admin_user_slash)
        assert resp_admin_user_slash.text == "admin_val"

        msg_admin_user_dc = IncomingMessage("test", "u1", "c1", "m8", f"::{admin_cmd.lstrip('/')} ok2", is_guest=False)
        resp_admin_user_dc = isolated_agent._handle_double_colon(msg_admin_user_dc)
        assert resp_admin_user_dc.text == "admin_val"
        assert len(admin_called) == 2

        # 属性 4: 默认 Agent 在拥有全局注册表时可以执行 global_cmd，但不能执行 isolated_cmd
        default_agent = _make_agent(command_registry=default_command_registry)
        resp_def_global = default_agent._handle_builtin(msg_global_slash)
        assert resp_def_global.text == "global_val"
        assert len(global_called) == 1

        resp_def_isolated = default_agent._handle_builtin(msg_iso_slash)
        assert resp_def_isolated.text == "ai loop fallback"
        assert len(isolated_called) == 2

    finally:
        default_command_registry.unregister(global_cmd)
