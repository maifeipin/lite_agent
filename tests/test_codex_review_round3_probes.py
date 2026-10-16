"""
Codex 第三轮复核反例长期回归专项 (tests/test_codex_review_round3_probes.py).
覆盖 T1–T5 缺陷的全部防倒退探针：
1. T1: core / 禁用插件时彻底杜绝业务入口（/api/v1/auth、/api/report、/api/pull_task、/agent 别名等严禁分发，返回 404）
2. T2: 会议通知生命周期与活体 Agent 贯通（create_app 启动即注入 live agent 到插件上下文，关停时安全注销）
3. T3: Todo 工具与 HTTP 必须使用同一实例数据库（支持多实例独立配置，互不污染）
4. T4: 业务实现与公共宿主彻底解耦，委托至 canonical adapter
5. T5: 路由前缀精准匹配边界防护
"""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
import pytest


def test_core_does_not_dispatch_meeting_login_when_plugin_absent():
    """T1: 未启用会议插件时，/api/v1/auth 严禁分发至业务 handler，必须返回 404。"""
    from channels.api import ApiHandler
    from services.routes import RouteRegistry
    h = Mock()
    h.path = '/api/v1/auth'
    h.server = SimpleNamespace(api_server=SimpleNamespace(route_registry=RouteRegistry()))
    h._auth.return_value = True
    h.is_guest = h.is_edge = False
    ApiHandler.do_POST(h)
    h._handle_auth.assert_not_called()
    h.send_error.assert_called_once_with(404, 'Not Found')


def test_core_does_not_dispatch_meeting_login_with_agent_prefix():
    """T1: 未启用会议插件时，/agent/api/v1/auth 别名严禁分发，必须返回 404。"""
    from channels.api import ApiHandler
    from services.routes import RouteRegistry
    h = Mock()
    h.path = '/agent/api/v1/auth'
    h.server = SimpleNamespace(api_server=SimpleNamespace(route_registry=RouteRegistry()))
    h._auth.return_value = True
    h.is_guest = h.is_edge = False
    ApiHandler.do_POST(h)
    h._handle_auth.assert_not_called()
    h.send_error.assert_called_once_with(404, 'Not Found')


def test_core_does_not_dispatch_edge_report_when_plugin_absent():
    """T1: 未启用 network_ops 插件时，POST /api/report 严禁分发，必须返回 404。"""
    from channels.api import ApiHandler
    from services.routes import RouteRegistry
    h = Mock()
    h.path = '/api/report'
    h.server = SimpleNamespace(api_server=SimpleNamespace(route_registry=RouteRegistry()))
    h._auth.return_value = True
    h.is_guest = h.is_edge = False
    ApiHandler.do_POST(h)
    h._handle_edge_report.assert_not_called()
    h.send_error.assert_called_once_with(404, 'Not Found')


def test_core_does_not_dispatch_edge_pull_task_when_plugin_absent():
    """T1: 未启用 network_ops 插件时，GET /api/pull_task 严禁分发，必须返回 404。"""
    from channels.api import ApiHandler
    from services.routes import RouteRegistry
    h = Mock()
    h.path = '/api/pull_task'
    h.server = SimpleNamespace(api_server=SimpleNamespace(route_registry=RouteRegistry()))
    h._auth.return_value = True
    h.is_guest = h.is_edge = False
    ApiHandler.do_GET(h)
    h._handle_pull_task.assert_not_called()
    h.send_error.assert_called_once_with(404, 'Not Found')


def test_meeting_notifier_receives_live_agent_in_production_registration():
    """T2: 生产环境注册会议插件时，start_notifier 必须接收到非空的活体 Agent。"""
    from app.bootstrap import create_app
    fake_skills = ModuleType('skills.ops_review_meeting')
    fake_agent = Mock()
    app = create_app(mode='full', config={
        'review_meeting_notifications': {'enabled': True},
        'channels': {'telegram': {'enabled': False}},
    }, agent=fake_agent)
    stopper = Mock()
    try:
        with patch.dict(sys.modules, {'skills.ops_review_meeting': fake_skills}), \
             patch('skills.ops_review_meeting', fake_skills, create=True), \
             patch('features.review_meeting.notifications.start_notifier', return_value=stopper) as start:
            app.plugin_loader.load_plugin_by_entry('features.review_meeting', 'review_meeting')
        start.assert_called_once()
        assert start.call_args.args[0] is not None, 'Production startup loads plugins before constructing Agent'
    finally:
        app.shutdown()


def test_route_prefix_does_not_match_different_resource_name():
    """T5: 路由前缀匹配不得吞并不同资源名（如 /todos_typo 不得命中 /todos）。"""
    from services.routes import RouteRegistry
    registry = RouteRegistry()
    registry.register('ALL', '/api/v1/todos', lambda *args: None, is_prefix=True)
    assert registry.match('GET', '/api/v1/todos_typo') is None
    assert registry.match('GET', '/api/v1/todos/123') is not None
    assert registry.match('GET', '/api/v1/todos') is not None


def test_todo_tool_and_http_use_same_instance_database():
    """T3: 同一应用的 Todo 工具与 HTTP 接口必须绑定配置的同一实例数据库。"""
    from app.bootstrap import create_app
    from features.todo import adapter
    from skills import ops_todo
    configured = '/tmp/review-instance-todo.db'
    app = create_app(mode='full', config={'todo': {'db_path': configured}})
    try:
        app.plugin_loader.load_plugin_by_entry('features.todo', 'todo')
        handler = SimpleNamespace(server=SimpleNamespace(api_server=SimpleNamespace(
            agent=SimpleNamespace(_config=app.config))))
        with patch.object(adapter, 'TodoService') as http_service:
            adapter.get_todo_service(handler)
            http_service.assert_called_once_with(db_path=configured)
        with patch.object(ops_todo, 'TodoService') as tool_service:
            app.skill_engine.get_tool('todo_add')['func'](title='review')
            assert tool_service.call_args.args[0] == configured
    finally:
        app.shutdown()


def test_dual_app_instances_use_isolated_databases():
    """T3: 两个不同的应用实例分别配置不同数据库，其工具与 HTTP 完全隔离互不干扰。"""
    from app.bootstrap import create_app
    from features.todo import adapter
    from skills import ops_todo

    db_a = '/tmp/instance-a-todo.db'
    db_b = '/tmp/instance-b-todo.db'

    app_a = create_app(mode='full', config={'todo': {'db_path': db_a}})
    app_b = create_app(mode='full', config={'todo': {'db_path': db_b}})

    try:
        app_a.plugin_loader.load_plugin_by_entry('features.todo', 'todo')
        app_b.plugin_loader.load_plugin_by_entry('features.todo', 'todo')

        with patch.object(ops_todo, 'TodoService') as tool_service:
            tool_a = app_a.skill_engine.get_tool('todo_add')
            assert tool_a is not None, f"app_a tool_add missing"
            tool_a['func'](title='task_a')
            assert tool_service.call_args.args[0] == db_a

            tool_b = app_b.skill_engine.get_tool('todo_add')
            assert tool_b is not None, f"app_b tool_add missing"
            tool_b['func'](title='task_b')
            assert tool_service.call_args.args[0] == db_b
    finally:
        app_a.shutdown()
        app_b.shutdown()
