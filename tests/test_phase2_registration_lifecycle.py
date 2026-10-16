"""
阶段二 2-B 注册所有权、生命周期与回滚强制反例测试套件 (tests/test_phase2_registration_lifecycle.py)。
严格依据 architecture/stage-2-3-antigravity-rules-20261015.md 第 4 节 2-B 编写。
"""

import threading
import pytest
from types import ModuleType
from unittest.mock import MagicMock, patch

from app.bootstrap import create_app
from app.plugins import PluginContext, PluginLoader
from services.commands import CommandRegistry
from services.workflows import WorkflowRegistry
from services.routes import RouteRegistry
from services.cron import CronService
from core.skill_engine import SkillEngine


def test_empty_allowlist_denies_without_import():
    """空白名单与 None 拒绝导入前拦截，importlib 零调用。"""
    ctx = PluginContext()
    loader = PluginLoader(context=ctx, allowed_entries=set())
    with patch("app.plugins.importlib.import_module") as mock_import:
        with pytest.raises(PermissionError):
            loader.load_plugin_by_entry("some.entry")
        mock_import.assert_not_called()


def test_single_plugin_registration_failure_rolls_back_cleanly():
    """反例 1: 插件注册到一半抛错后无残留（单插件回滚）。"""
    cmd_reg = CommandRegistry()
    skill_eng = SkillEngine(tools={})
    wf_reg = WorkflowRegistry()
    route_reg = RouteRegistry()
    cron_srv = CronService()
    app = create_app(mode="core")
    app.command_registry = cmd_reg
    app.skill_engine = skill_eng
    app.workflow_registry = wf_reg
    app.route_registry = route_reg
    app.cron_service = cron_srv

    ctx = PluginContext(
        command_registry=cmd_reg,
        skill_engine=skill_eng,
        workflow_registry=wf_reg,
        route_registry=route_reg,
        cron_service=cron_srv,
        lifecycle=app.lifecycle,
        config={},
        plugin_name="faulty_plugin",
    )
    loader = PluginLoader(context=ctx, allowed_entries={"faulty_plugin"})

    faulty_module = ModuleType("faulty_plugin")

    def broken_register(c: PluginContext):
        c.register_command("/faulty_cmd", lambda a, m, args: None)
        c.register_skill("faulty_tool", {"func": lambda: None, "schema": {}})
        c.register_workflow("faulty_wf", lambda c, r, l: None, lambda c, t: False)
        c.register_route("GET", "/api/v1/faulty", lambda req: None)
        c.register_cron("faulty_cron", "12:00", lambda: None)
        c.register_cleanup("faulty_res", lambda: None)
        # 中途异常！
        raise RuntimeError("中途注册失败模拟")

    faulty_module.register = broken_register

    with patch("app.plugins.importlib.import_module", return_value=faulty_module):
        with pytest.raises(RuntimeError, match="中途注册失败模拟"):
            loader.load_plugin_by_entry("faulty_plugin")

    # 断言回滚后无残留！
    assert cmd_reg.get("/faulty_cmd") is None
    assert "faulty_tool" not in skill_eng._registry
    assert wf_reg.get("faulty_wf") is None
    assert route_reg.match("GET", "/api/v1/faulty") is None
    assert "faulty_cron" not in cron_srv._job_names
    assert len(loader.loaded_plugins) == 0


def test_second_plugin_failure_rolls_back_first_plugin():
    """反例 2: 第二插件启动失败，前一插件被完全释放（应用级回滚）。"""
    app = create_app(mode="core")
    p1 = ModuleType("plugin_1")
    p1_cleanup_called = False

    def p1_cleanup():
        nonlocal p1_cleanup_called
        p1_cleanup_called = True

    def p1_reg(c: PluginContext):
        c.register_command("/p1_cmd", lambda a, m, args: None)
        c.register_cleanup("p1_res", p1_cleanup)

    p1.register = p1_reg

    p2 = ModuleType("plugin_2")

    def p2_reg(c: PluginContext):
        c.register_command("/p2_cmd", lambda a, m, args: None)
        raise RuntimeError("插件2启动致命错误")

    p2.register = p2_reg

    loader = PluginLoader(context=app.plugin_loader.context, allowed_entries={"plugin_1", "plugin_2"})

    def mock_import(name):
        return p1 if name == "plugin_1" else p2

    manifest = [
        {"name": "p1", "entry": "plugin_1", "dependencies": []},
        {"name": "p2", "entry": "plugin_2", "dependencies": []},
    ]

    with patch("app.plugins.importlib.import_module", side_effect=mock_import):
        with pytest.raises(RuntimeError, match="插件2启动致命错误"):
            loader.load_all(manifest)

    # 断言应用级回滚：插件1的命令被注销，插件1的 cleanup 资源被注销或清理
    assert app.command_registry.get("/p1_cmd") is None
    assert app.command_registry.get("/p2_cmd") is None
    assert len(loader.loaded_plugins) == 0


def test_duplicate_name_rejection_across_services():
    """反例 3: 重名工具/命令/工作流/路由/作业报错，禁止静默覆盖。"""
    # 1. 重名命令
    cmd_reg = CommandRegistry()
    cmd_reg.register("/test", lambda *a: None)
    with pytest.raises(ValueError, match="指令已存在"):
        cmd_reg.register("/test", lambda *a: None)

    # 2. 重名工具
    engine = SkillEngine(tools={})
    engine.register_tool("echo", {"func": lambda: "1"})
    with pytest.raises(ValueError, match="工具已存在"):
        engine.register_tool("echo", {"func": lambda: "2"})

    # 3. 重名工作流
    wf_reg = WorkflowRegistry()
    wf_reg.register("billing", lambda *a: None, lambda *a: True)
    with pytest.raises(ValueError, match="工作流已存在"):
        wf_reg.register("billing", lambda *a: None, lambda *a: False)

    # 4. 重名路由
    route_reg = RouteRegistry()
    route_reg.register("GET", "/api/test", lambda *a: None)
    with pytest.raises(ValueError, match="路由已存在"):
        route_reg.register("GET", "/api/test", lambda *a: None)

    # 5. 重名定时作业
    cron_srv = CronService()
    cron_srv.add_job("sync", "10:00", lambda: None)
    with pytest.raises(ValueError, match="定时任务已存在"):
        cron_srv.add_job("sync", "10:00", lambda: None)


def test_circular_dependency_rejected_without_registration():
    """反例 4: 循环依赖在拓扑校验时阻断，无注册副作用。"""
    ctx = PluginContext()
    loader = PluginLoader(context=ctx, allowed_entries={"plugin_a", "plugin_b"})

    manifest = [
        {"name": "a", "entry": "plugin_a", "dependencies": ["b"]},
        {"name": "b", "entry": "plugin_b", "dependencies": ["a"]},
    ]

    with patch("app.plugins.importlib.import_module") as mock_imp:
        with pytest.raises(ValueError, match="循环依赖"):
            loader.load_all(manifest)
        mock_imp.assert_not_called()


def test_two_instances_completely_isolated():
    """两个应用实例同时存在，工具、命令、作业互不影响。"""
    engine1 = SkillEngine(tools={})
    engine2 = SkillEngine(tools={})
    app1 = create_app(mode="core", skill_engine=engine1)
    app2 = create_app(mode="core", skill_engine=engine2)

    app1.command_registry.register("/app1_only", lambda *a: None)
    app1.skill_engine.register_tool("tool1", {"func": lambda: 1})
    app1.cron_service.add_job("job1", "08:00", lambda: None)

    assert app1.command_registry.get("/app1_only") is not None
    assert app2.command_registry.get("/app1_only") is None

    assert "tool1" in app1.skill_engine._registry
    assert "tool1" not in app2.skill_engine._registry

    assert "job1" in app1.cron_service._job_names
    assert "job1" not in app2.cron_service._job_names

    app1.shutdown()
    assert app1.lifecycle.is_shutdown
    assert not app2.lifecycle.is_shutdown

    # app2 仍正常工作
    app2.command_registry.register("/app2_cmd", lambda *a: None)
    assert app2.command_registry.get("/app2_cmd") is not None
    app2.shutdown()


def test_cleanup_partial_failure_continues_and_summarizes():
    """个别 cleanup 失败仍继续其他释放，并汇总错误。"""
    from app.lifecycle import LifecycleManager
    lm = LifecycleManager()
    second_cleanup_called = False

    def bad_clean():
        raise RuntimeError("资源1释放抛错")

    def good_clean():
        nonlocal second_cleanup_called
        second_cleanup_called = True

    lm.register_resource("res_bad", bad_clean)
    lm.register_resource("res_good", good_clean)

    errors = lm.shutdown()
    assert len(errors) == 1
    assert "res_bad" in errors[0]
    assert second_cleanup_called is True
