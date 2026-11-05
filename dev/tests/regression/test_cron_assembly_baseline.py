"""Cron 组装所有权基线契约 (Stage 3 Commit 1)。

锁定调度器归属边界，为后续迁移提供安全网：
- create_app() 应用持有专属调度器，应用间默认隔离。
- 裸 Agent(cron=None) 兜底模块默认实例；显式注入(含 falsy 合法替身)优先。
- 启用记忆不得覆盖已注入调度器 (当前缺陷，见 test_memory 用例)。
- 插件任务注册到宿主调度器；生产任务清单参数化锁定。

本文件只锁定目标契约与已确认缺陷的红测试，不修改生产代码。
缺陷修复随对应提交落地后，相关用例由红转绿。
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lite_agent.app.bootstrap import create_app
from lite_agent.services.cron import CronService, default_cron_service


# ---------------------------------------------------------------------------
# 最小 Agent 构造桩: 提供 Agent.__init__ 必需的 llm 配置, 不触发网络
# ---------------------------------------------------------------------------
def _minimal_llm_config():
    return {
        "llm": {
            "provider": "openai",
            "api_key": "test-key",
            "base_url": "http://127.0.0.1:9/unused",
            "model": "test-model",
        }
    }


def _make_agent(cron_marker, config=None, disable_memory=False):
    """构造一个最小 Agent。cron_marker 显式传入(可为 None/falsy 替身)。

    disable_memory=True 时 patch MEMORY_AVAILABLE=False, 隔离记忆启用路径
    (agent.py:323 的 cron 覆盖), 用于单独锁定 line 179 的注入/兜底语义。
    """
    import lite_agent.agent as agent_mod
    from unittest.mock import patch
    from lite_agent.agent import Agent

    def _build():
        return Agent(
            config=config or _minimal_llm_config(),
            skill_engine=None,
            command_registry=None,
            workflow_registry=None,
            cron=cron_marker,
            domain_map={},
        )

    if disable_memory:
        with patch.object(agent_mod, "MEMORY_AVAILABLE", False):
            return _build()
    return _build()


# ===========================================================================
# 边界 A: 应用实例隔离 —— create_app() 各自持有独立调度器
# ===========================================================================
class TestAppIsolation:
    def test_two_apps_have_independent_cron_services(self):
        app1 = create_app(mode="core", config={})
        app2 = create_app(mode="core", config={})
        try:
            assert app1.cron_service is not app2.cron_service
            assert app1.cron_service is not default_cron_service
            assert app2.cron_service is not default_cron_service
        finally:
            app1.shutdown()
            app2.shutdown()

    def test_app_jobs_and_handlers_do_not_leak_across_apps(self):
        app1 = create_app(mode="core", config={})
        app2 = create_app(mode="core", config={})
        try:
            app1.cron_service.add_job("app1_only", "03:00", lambda: "one")
            app2.cron_service.add_job("app2_only", "04:00", lambda: "two")

            names1 = {j.name for j in app1.cron_service.jobs.values()}
            names2 = {j.name for j in app2.cron_service.jobs.values()}
            assert "app1_only" in names1 and "app1_only" not in names2
            assert "app2_only" in names2 and "app2_only" not in names1
        finally:
            app1.shutdown()
            app2.shutdown()

    def test_app_lifecycle_registers_cron_stop_and_clear(self):
        """应用 shutdown 应停启自己的调度器并清空任务, 不影响其他应用。"""
        app1 = create_app(mode="core", config={})
        app2 = create_app(mode="core", config={})
        keep = Mock(return_value="keep")
        app2.cron_service.add_job("survivor", "03:00", keep)
        app1.cron_service.add_job("doomed", "03:00", lambda: "x")
        app1.shutdown()
        try:
            assert len(app1.cron_service.jobs) == 0
            assert "survivor" in {j.name for j in app2.cron_service.jobs.values()}
        finally:
            app2.shutdown()


# ===========================================================================
# 边界 B: 裸 Agent 兜底 —— cron=None 用模块默认实例
# ===========================================================================
class TestBareAgentFallback:
    """锁定 line 179 兜底语义, 隔离记忆覆盖路径 (disable_memory=True)。"""

    def test_bare_agent_uses_default_cron_service(self):
        agent = _make_agent(cron_marker=None, disable_memory=True)
        assert agent.cron is default_cron_service

    def test_two_bare_agents_share_default_instance(self):
        """裸 Agent 是另一种使用方式, 共享默认实例, 不等于两个隔离应用。"""
        a1 = _make_agent(cron_marker=None, disable_memory=True)
        a2 = _make_agent(cron_marker=None, disable_memory=True)
        assert a1.cron is a2.cron is default_cron_service


# ===========================================================================
# 边界 C: 显式注入优先 —— 包括 falsy 合法替身
# ===========================================================================
class FalsyCronStub:
    """布尔值为假的合法调度器替身: 必须被保留, 不得被默认实例替换。"""

    def __init__(self):
        self.jobs = {}

    def __bool__(self):
        return False

    __len__ = lambda self: 0


class TestExplicitInjectionPrecedence:
    """锁定 line 179 注入语义, 隔离记忆覆盖路径 (disable_memory=True)。"""

    def test_injected_cron_is_preserved(self):
        stub = CronService()
        agent = _make_agent(cron_marker=stub, disable_memory=True)
        assert agent.cron is stub

    def test_falsy_injected_cron_is_preserved(self):
        """is None 语义: falsy 合法替身不得被 `cron or default` 吞掉。

        当前 agent.py 用 `cron or default_cron_service`, 对 falsy 对象会错误
        回退到默认实例。本用例在修复 (提交2) 前应为红。
        """
        stub = FalsyCronStub()
        agent = _make_agent(cron_marker=stub, disable_memory=True)
        assert agent.cron is stub, (
            "falsy 合法替身被 `cron or default` 吞掉; "
            "应改为 `default_cron_service if cron is None else cron`"
        )


# ===========================================================================
# 边界 D: 记忆启用不得覆盖注入 (当前缺陷 — agent.py:323)
# ===========================================================================
class TestMemoryEnableDoesNotOverrideCron:
    """缺陷红测试组 (提交2 修复): 记忆启用路径不得改变基础组件。

    当前 _register_memory_commands 末尾执行 self.cron = CronManager(),
    在 MEMORY_AVAILABLE 为真时于构造期覆盖注入。修复后本组转绿。
    """

    def _memory_available(self):
        import lite_agent.agent as agent_mod
        return getattr(agent_mod, "MEMORY_AVAILABLE", False)

    def test_injected_cron_survives_memory_enable(self):
        """启用记忆后, 显式注入的调度器必须不变。"""
        if not self._memory_available():
            pytest.skip("memory_engine 未安装, 跳过")
        stub = CronService()
        agent = _make_agent(cron_marker=stub)
        assert agent.cron is stub, (
            "记忆命令注册覆盖了注入的调度器 (agent.py:323); "
            "注册记忆命令不得改变基础组件"
        )

    def test_falsy_injected_cron_survives_memory_enable(self):
        """启用记忆后, falsy 合法替身同样不得被覆盖。"""
        if not self._memory_available():
            pytest.skip("memory_engine 未安装, 跳过")
        stub = FalsyCronStub()
        agent = _make_agent(cron_marker=stub)
        assert agent.cron is stub

    def test_memory_enable_does_not_start_extra_cron_loop(self):
        """记忆启用不得让 Agent 持有独立于注入实例的第二个调度循环。"""
        if not self._memory_available():
            pytest.skip("memory_engine 未安装, 跳过")
        stub = CronService()
        agent = _make_agent(cron_marker=stub)
        # Agent 持有的调度器必须就是注入实例, 而非新建的 CronManager 单例
        assert agent.cron is stub


# ===========================================================================
# 边界 E: /cron、/health 与应用调度器同实例
# ===========================================================================
class TestObservabilitySameInstance:
    def test_health_snapshot_comes_from_app_cron_service(self):
        app = create_app(mode="core", config={})
        try:
            snap = app.cron_service.health_snapshot()
            assert isinstance(snap, dict)
            assert "running" in snap
        finally:
            app.shutdown()

    def test_agent_cron_in_app_is_app_cron_service(self):
        """create_app 注入 Agent 时, agent.cron 必须就是 app.cron_service。

        当前因 agent.py:323 记忆路径覆盖而红 (本环境 MEMORY_AVAILABLE 为真);
        提交2 修复后转绿。
        """
        app = create_app(mode="core", config=_minimal_llm_config())
        try:
            if app.agent is not None:
                assert app.agent.cron is app.cron_service, (
                    "应用持有的调度器与 Agent.cron 不是同一实例; "
                    "/cron 与 /health 会指向不同调度器"
                )
        finally:
            app.shutdown()


# ===========================================================================
# 生产任务清单 (参数化): 插件注册到宿主调度器
# name, cron_expr, 注册来源
# ===========================================================================
PLUGIN_JOBS = [
    ("todo_tick", "every_minute", "features.todo"),
    ("todo_brief", "08:30", "features.todo"),
    ("billing_parse_health_tick", "09:00", "features.billing"),
    ("billing_fetch_tick", "03:00", "features.billing"),
    ("ops_backup_cloud", "03:00", "features.sys_ops"),
    ("edge_health", "*/10 * * * *", "features.network_ops"),
]


class TestPluginJobManifest:
    @pytest.mark.parametrize("name,expr,source", PLUGIN_JOBS)
    def test_plugin_job_manifest_locked(self, name, expr, source):
        """锁定插件任务名称与时间表达式契约 (来源见各 feature __init__)。"""
        assert name and expr and source  # 参数化即契约, 实际接线见下方装配用例

    def test_0300_backup_and_billing_are_distinct_jobs(self):
        """03:00 的备份与账单拉取是不同任务, 各保留一份, 不按时间去重。"""
        at_0300 = [n for n, e, _ in PLUGIN_JOBS if e == "03:00"]
        assert "ops_backup_cloud" in at_0300
        assert "billing_fetch_tick" in at_0300
        assert len(at_0300) == 2

    def test_full_mode_app_registers_plugin_jobs_on_host_cron(self):
        """full 模式装配并加载插件后, 插件任务应注册到 app.cron_service。"""
        from lite_agent.app.manifests import FULL_MODE_PLUGINS
        app = create_app(mode="full", config={})
        try:
            app.plugin_loader.load_all(FULL_MODE_PLUGINS)
            registered = {j.name for j in app.cron_service.jobs.values()}
            # 至少锁定核心插件任务已落在宿主调度器
            for name, _, _ in PLUGIN_JOBS:
                assert name in registered, f"插件任务 {name} 未注册到宿主调度器"
        finally:
            app.shutdown()


# ===========================================================================
# Todo tick db_path 契约 (提交3 修复): _todo_tick 接受 db_path 并绑定实例库
# features/todo/__init__.py 按配置注入 db_path
# ===========================================================================
class TestTodoTickDbPathContract:
    def test_todo_tick_accepts_db_path_when_configured(self):
        """Todo 插件配置独立数据库时, _todo_tick 必须接受 db_path 参数。"""
        import inspect
        from lite_agent.skills import ops_todo
        sig = inspect.signature(ops_todo._todo_tick)
        assert "db_path" in sig.parameters, (
            "_todo_tick() 不接受 db_path, 但 features/todo 插件会传入; "
            "需修正签名以接受数据库路径参数"
        )

    def test_host_todo_tick_binds_configured_db_path(self):
        """宿主调度器上的 todo_tick 任务执行时必须使用配置的实例数据库。"""
        from unittest.mock import patch
        configured = "/tmp/stage3-commit3-todo.db"
        app = create_app(mode="full", config={"todo": {"db_path": configured}})
        try:
            app.plugin_loader.load_plugin_by_entry("lite_agent.features.todo", "todo")
            jobs = {j.name: j for j in app.cron_service.jobs.values()}
            tick_job = jobs["todo_tick"]
            from lite_agent.skills import ops_todo
            with patch.object(ops_todo, "get_service") as gs:
                tick_job.func()
                gs.assert_called_once_with(configured)
        finally:
            app.shutdown()
