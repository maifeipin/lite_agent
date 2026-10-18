from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

@pytest.mark.parametrize('method', ['GET', 'POST', 'PATCH', 'DELETE'])
def test_registered_business_route_must_authenticate_before_handler(method):
    from lite_agent.channels.api import ApiHandler
    from lite_agent.services.routes import RouteRegistry
    registry = RouteRegistry()
    business = Mock()
    registry.register(method, '/api/v1/todos', business, auth_required=True)
    handler = Mock()
    handler.path = '/api/v1/todos'
    handler.is_edge = False
    handler.is_guest = False
    handler.server = SimpleNamespace(api_server=SimpleNamespace(route_registry=registry))
    handler._auth.return_value = False
    getattr(ApiHandler, 'do_' + method)(handler)
    business.assert_not_called()
    handler._auth.assert_called_once()


def test_configured_skill_cron_runs_registered_instance_function():
    from lite_agent.cli import _register_configured_cron_jobs
    from lite_agent.core.skill_engine import SkillEngine
    from lite_agent.services.cron import CronService
    engine = SkillEngine(tools={}, auto_load=False)
    work = Mock(return_value='done')
    engine.register_tool('review_cron', {'func': work, 'schema': {}})
    app = SimpleNamespace(skill_engine=engine, cron_service=CronService())
    _register_configured_cron_jobs(app, None, {'cron_jobs': [
        {'name': 'review_cron', 'time': '09:00', 'skill': 'review::review_cron', 'kwargs': {'limit': 3}}
    ]})
    job = next(iter(app.cron_service.jobs.values()))
    assert job.func() == 'done'
    work.assert_called_once_with(limit=3)


def test_legacy_engine_does_not_absorb_tools_registered_after_creation():
    from lite_agent.core.skill_engine import SkillEngine, _skill_registry
    with patch.object(SkillEngine, '_load_skills'):
        engine = SkillEngine(auto_load=True)
    try:
        _skill_registry['review_cross_instance_tool'] = {'func': lambda: None, 'schema': {}}
        assert 'review_cross_instance_tool' not in engine._registry
    finally:
        _skill_registry.pop('review_cross_instance_tool', None)


def test_todo_push_brief_legacy_url_dispatches_brief_handler():
    # Load the plugin registration itself against fake skills: no DB/import side effects.
    from lite_agent.features.todo import register
    import sys
    from types import ModuleType
    fake = ModuleType('lite_agent.skills.ops_todo')
    fake._todo_tick = lambda: None
    fake.todo_push_brief = lambda: None
    captured = {}
    ctx = SimpleNamespace(
        register_skill=lambda *a, **k: None,
        register_cron=lambda *a, **k: None,
        register_route=lambda method, pattern, handler, **kw: captured.update(handler=handler),
    )
    import lite_agent.skills
    with patch.dict(sys.modules, {'lite_agent.skills.ops_todo': fake, 'skills.ops_todo': fake}), \
         patch.object(lite_agent.skills, 'ops_todo', fake, create=True), \
         patch('importlib.reload', lambda mod: mod):
        register(ctx)
    handler = Mock()
    handler.command = 'POST'
    captured['handler'](handler, SimpleNamespace(path='/api/v1/todos/brief/push', query=''))
    handler._handle_post_todo_brief_push.assert_called_once()
    handler._handle_post_todo.assert_not_called()


@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.parametrize('identity', ['guest', 'edge'])
def test_registered_business_route_rejects_guest_and_edge(method, identity):
    from lite_agent.channels.api import ApiHandler
    from lite_agent.services.routes import RouteRegistry
    registry = RouteRegistry()
    business = Mock()
    registry.register(method, '/api/v1/todos', business, auth_required=True)
    handler = Mock()
    handler.path = '/api/v1/todos'
    handler.server = SimpleNamespace(api_server=SimpleNamespace(route_registry=registry))
    handler.is_guest = identity == 'guest'
    handler.is_edge = identity == 'edge'
    handler._auth.return_value = True
    getattr(ApiHandler, 'do_' + method)(handler)
    business.assert_not_called()


def test_cron_clear_releases_job_name_reservations():
    from lite_agent.services.cron import CronService
    cron = CronService()
    cron.add_job('review_job', '03:00', lambda: None)
    cron.clear()
    assert cron._job_names == {}
    cron.add_job('review_job', '03:00', lambda: None)
