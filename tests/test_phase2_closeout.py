"""Independent stage-two closeout regressions."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from services.routes import RouteRegistry


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/api/v1/todos", True),
        ("/api/v1/todos/", True),
        ("/api/v1/todos/123", True),
        ("/api/v1/todos_typo", False),
        ("/api/v1/todos-other", False),
    ],
)
def test_prefix_route_respects_path_segment_boundary(path, expected):
    registry = RouteRegistry()
    registry.register("GET", "/api/v1/todos", lambda *args: None, is_prefix=True)
    assert (registry.match("GET", path) is not None) is expected


@pytest.mark.parametrize('prefix', ['/', '::'])
def test_instance_admin_command_denies_guest_before_handler(prefix):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from agent import Agent
    from services.commands import CommandRegistry

    registry = CommandRegistry()
    protected = Mock(return_value='protected action')
    registry.register('/closeout_admin', protected, admin_only=True, guest_ok=False)
    agent = Agent.__new__(Agent)
    agent.command_registry = registry
    message = SimpleNamespace(text=prefix + 'closeout_admin', is_guest=True)
    dispatch = agent._handle_builtin if prefix == '/' else agent._handle_double_colon
    response = dispatch(message)
    protected.assert_not_called()
    assert '权限不足' in response.text


@pytest.mark.parametrize('prefix', ['/', '::'])
def test_explicit_command_registry_never_executes_foreign_global_command(prefix):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from agent import Agent
    from contracts.message import AgentResponse
    from services.commands import CommandRegistry, default_command_registry

    foreign = Mock(return_value='foreign action')
    default_command_registry.register('/closeout_foreign', foreign, guest_ok=True)
    try:
        agent = Agent.__new__(Agent)
        agent.command_registry = CommandRegistry()
        agent._run_ai_loop = Mock(return_value=AgentResponse('unknown command'))
        message = SimpleNamespace(text=prefix + 'closeout_foreign', is_guest=False)
        dispatch = agent._handle_builtin if prefix == '/' else agent._handle_double_colon
        dispatch(message)
        foreign.assert_not_called()
    finally:
        default_command_registry.unregister('/closeout_foreign')


def _closeout_full_app(config=None, channels=None):
    from app.bootstrap import create_app
    from app.manifests import FULL_MODE_PLUGINS
    app = create_app(mode='full', config=config or {}, agent=SimpleNamespace(channels=channels or []))
    app.plugin_loader.load_all(FULL_MODE_PLUGINS)
    return app


def test_rss_cron_preserves_configured_telegram_recipient():
    ch = SimpleNamespace(name='telegram', send_to=Mock(return_value=True))
    app = _closeout_full_app({'channels': {'telegram': {'admin_chat_id': '123456'}}}, [ch])
    try:
        with patch('features.content_ops.rss_service.rss_brief', return_value='review RSS'):
            app.cron_service.get_handler('ops_rss::rss_push')()
        ch.send_to.assert_called_once()
        assert ch.send_to.call_args.args[0] == '123456'
    finally:
        app.shutdown()


def test_rss_cron_does_not_report_success_when_send_fails():
    ch = SimpleNamespace(name='feishu', send_to=Mock(return_value=False))
    app = _closeout_full_app({'channels': {'feishu': {'admin_open_id': 'ou_review'}}}, [ch])
    try:
        with patch('features.content_ops.rss_service.rss_brief', return_value='review RSS'):
            result = app.cron_service.get_handler('ops_rss::rss_push')()
        assert '已推送' not in result
    finally:
        app.shutdown()


def test_configured_precompute_and_plugin_default_do_not_duplicate_slot():
    from main import _register_configured_cron_jobs
    cfg = {'cron_jobs': [{'name': 'RSS 预计算', 'time': '09:50', 'skill': 'ops_rss::rss_precompute'}]}
    app = _closeout_full_app(cfg)
    try:
        _register_configured_cron_jobs(app, app.agent, cfg)
        same_slot = [j for j in app.cron_service.jobs.values() if j.cron_expr == '09:50']
        assert len(same_slot) == 1, [j.name for j in same_slot]
    finally:
        app.shutdown()


def test_job_handler_duplicate_does_not_overwrite_existing_owner():
    from services.cron import CronService
    cron = CronService()
    first = lambda: 'first'
    cron.register_handler('review::job', first)
    with pytest.raises(ValueError):
        cron.register_handler('review::job', lambda: 'second')
    assert cron.get_handler('review::job') is first
