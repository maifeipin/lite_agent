from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest


@pytest.mark.parametrize("bad_job", [
    {"name": "empty-times", "times": [], "skill": "ops_x::run"},
    {"name": "reversed-range", "time_range": {"start": 20, "end": 8}, "skill": "ops_x::run"},
    {"name": "range-out-of-bounds", "time_range": {"start": 0, "end": 24}, "skill": "ops_x::run"},
    {"name": "minute-out-of-bounds", "time_range": {"start": 1, "end": 2, "minute": 60}, "skill": "ops_x::run"},
    {"name": "non-int-range", "time_range": {"start": "0", "end": 1}, "skill": "ops_x::run"},
])
def test_invalid_time_config_rejected_without_touching_existing_jobs(bad_job):
    """展开后无时间槽的配置必须拒绝 (不得静默注册零任务), 且失败不改动既有任务。"""
    from lite_agent.cli import _register_configured_cron_jobs
    from lite_agent.services.cron import CronService
    cron = CronService()
    cron.add_job("pre-existing", "03:00", lambda: None)
    app = SimpleNamespace(cron_service=cron)
    with pytest.raises(ValueError):
        _register_configured_cron_jobs(app, None, {"cron_jobs": [bad_job]})
    assert {j.name for j in cron.jobs.values()} == {"pre-existing"}


def test_valid_single_hour_range_and_multi_times_still_expand():
    """边界正例: start==end 单小时合法; 非空 times 正常展开为多个任务名。"""
    from lite_agent.cli import _register_configured_cron_jobs
    from lite_agent.services.cron import CronService
    cron = CronService()
    app = SimpleNamespace(cron_service=cron)
    _register_configured_cron_jobs(app, None, {"cron_jobs": [
        {"name": "single-hour", "time_range": {"start": 5, "end": 5, "minute": 30},
         "skill": "ops_x::run"},
        {"name": "multi", "times": ["09:00", "18:00"], "skill": "ops_x::run"},
    ]})
    assert "single-hour" in cron._job_names
    assert "multi#09:00" in cron._job_names
    assert "multi#18:00" in cron._job_names


def test_failed_registration_executes_cleanup():
    from lite_agent.app.bootstrap import create_app
    from lite_agent.app.plugins import PluginLoader
    app = create_app(mode='core')
    cleanup = Mock()
    def register(ctx):
        ctx.register_cleanup('open_resource', cleanup)
        raise RuntimeError('after acquiring resource')
    plugin = ModuleType('review_plugin')
    plugin.register = register
    loader = PluginLoader(app.plugin_loader.context, {'review_plugin'})
    try:
        with patch('lite_agent.app.plugins.importlib.import_module', return_value=plugin):
            try:
                loader.load_plugin_by_entry('review_plugin')
            except RuntimeError:
                pass
        cleanup.assert_called_once()
    finally:
        app.shutdown()


def test_core_http_todo_does_not_dispatch_business():
    from lite_agent.channels.api import ApiHandler
    from lite_agent.services.routes import RouteRegistry
    handler = Mock()
    handler.path = '/api/v1/todos'
    handler.is_guest = False
    handler.is_edge = False
    handler._auth.return_value = True
    handler.server = SimpleNamespace(api_server=SimpleNamespace(route_registry=RouteRegistry()))
    ApiHandler.do_GET(handler)
    handler._handle_todos.assert_not_called()
    handler.send_error.assert_called_once_with(404, 'Not Found')


def test_configured_skill_cron_is_preserved():
    from lite_agent.cli import _register_configured_cron_jobs
    from lite_agent.services.cron import CronService
    cron = CronService()
    app = SimpleNamespace(cron_service=cron)
    _register_configured_cron_jobs(app, None, {'cron_jobs': [
        {'name': 'rss-pre', 'time': '09:00', 'skill': 'ops_rss::rss_precompute'}
    ]})
    assert 'rss-pre' in cron._job_names


def test_full_engines_do_not_share_tool_registry():
    from lite_agent.core.skill_engine import SkillEngine
    with patch.object(SkillEngine, '_load_skills'):
        first = SkillEngine(auto_load=True)
        second = SkillEngine(auto_load=True)
    assert first._registry is not second._registry


def test_default_cron_service_is_stable_shared_fallback():
    """裸 Agent 兜底的模块级调度器是稳定单对象: 重复获取不重建、不清空已注册任务。"""
    from lite_agent.services import cron as cron_module
    first = cron_module.default_cron_service
    first.add_job('review-job', '03:00', lambda: None)
    try:
        from lite_agent.services.cron import default_cron_service as second
        assert second is first
        assert 'review-job' in second._job_names
    finally:
        first.remove_job_by_name('review-job')


def test_shutdown_unregisters_plugin_tools_and_jobs():
    from lite_agent.app.bootstrap import create_app
    from lite_agent.app.plugins import PluginLoader
    app = create_app(mode='core')
    def register(ctx):
        ctx.register_skill('review_tool', {'func': lambda: None, 'schema': {}})
        ctx.register_cron('review_job', '03:00', lambda: None)
    plugin = ModuleType('review_plugin')
    plugin.register = register
    loader = PluginLoader(app.plugin_loader.context, {'review_plugin'})
    with patch('lite_agent.app.plugins.importlib.import_module', return_value=plugin):
        loader.load_plugin_by_entry('review_plugin')
    app.shutdown()
    assert 'review_tool' not in app.skill_engine._registry
    assert 'review_job' not in app.cron_service._job_names


def test_agent_workflow_match_uses_injected_registry():
    from lite_agent.agent import Agent
    from lite_agent.services.workflows import WorkflowRegistry
    registry = WorkflowRegistry()
    registry.register('review_wf', lambda *args: None, lambda cfg, text: text == 'review_unique_request')
    # Agent.handle calls self._matches_billing_workflow: bind the real method
    # to an object carrying the injected registry, without initializing DB/model.
    agent = Agent.__new__(Agent)
    agent.workflow_registry = registry
    agent._workflow_registry = registry
    assert agent._matches_billing_workflow({}, 'review_unique_request')
