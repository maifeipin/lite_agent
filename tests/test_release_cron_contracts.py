"""Offline regressions for the five production cron compatibility gaps."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.bootstrap import create_app
from core.skill_engine import SkillEngine
from main import _register_configured_cron_jobs
from services.cron import CronService


def run_job(app, name):
    return next(j.func for j in app.cron_service.jobs.values() if j.name == name)()


def bare_app(tools=None):
    return SimpleNamespace(cron_service=CronService(), skill_engine=SkillEngine(tools=tools or {}, auto_load=False))


def test_comment_metadata_does_not_hide_a_valid_job():
    app = bare_app({'probe': {'func': lambda: 'called'}})
    _register_configured_cron_jobs(app, None, {'cron_jobs': [
        {'_comment': 'placeholder'},
        {'name': 'real', '_comment': 'description', 'time': '04:30', 'skill': 'probe'},
    ]})
    assert run_job(app, 'real') == 'called'


@pytest.mark.parametrize('job', [
    {'name': 'bad', '_comment': 'description', 'skill': 'probe'},
    {'name': 'bad', '_comment': 'description', 'time': '04:30'},
])
def test_comment_does_not_suppress_invalid_job_errors(job):
    with pytest.raises(ValueError):
        _register_configured_cron_jobs(bare_app(), None, {'cron_jobs': [job]})


@pytest.mark.parametrize('push,text,color', [
    (False, 'result', None), (None, 'result', None),
    (True, '', None), (True, '✅ done', None), (True, '(no result)', None),
    (True, 'actual output', 'blue'), (True, '❌ failed', 'red'),
    (True, '⚠️ warning', 'red'), (True, '✉️ mail', 'red'),
    (True, 'Error: failed', 'red'),
])
def test_generic_output_delivery_rules_and_return_value(monkeypatch, push, text, color):
    send = Mock(return_value=True)
    monkeypatch.setattr('services.owner_delivery.send_owner_response', send)
    fn = Mock(return_value=text)
    app = bare_app({'probe': {'func': fn}})
    agent = SimpleNamespace(channels=['host-channel'])
    job = {'name': 'report', 'time': '07:00', 'skill': 'legacy::probe', 'kwargs': {'limit': 3}}
    if push is not None:
        job['push_on_output'] = push
    cfg = {'channels': {'wechat': {'admin_wxid': 'test-owner'}}, 'cron_jobs': [job]}
    _register_configured_cron_jobs(app, agent, cfg)
    assert run_job(app, 'report') == text
    fn.assert_called_once_with(limit=3)
    if color:
        send.assert_called_once()
        channels, channel_config, response = send.call_args.args
        assert channels == agent.channels and channel_config == cfg['channels']
        assert response.text == text and response.title == 'report' and response.color == color
    else:
        send.assert_not_called()


def test_job_output_options_are_bound_per_job_and_errors_propagate(monkeypatch):
    send = Mock(return_value=True)
    monkeypatch.setattr('services.owner_delivery.send_owner_response', send)
    app = bare_app({'probe': {'func': lambda: 'output'}, 'broken': {'func': Mock(side_effect=RuntimeError('broken'))}})
    _register_configured_cron_jobs(app, SimpleNamespace(channels=[]), {'cron_jobs': [
        {'name': 'first', 'time': '07:00', 'skill': 'probe', 'push_on_output': True},
        {'name': 'second', 'time': '08:00', 'skill': 'probe', 'push_on_output': False},
        {'name': 'broken', 'time': '09:00', 'skill': 'broken', 'push_on_output': True},
    ]})
    run_job(app, 'first'); run_job(app, 'second')
    assert send.call_count == 1 and send.call_args.args[2].title == 'first'
    with pytest.raises(RuntimeError, match='broken'):
        run_job(app, 'broken')
    assert send.call_count == 1


def test_mail_cron_resolves_with_current_host_and_no_second_push(monkeypatch):
    fn = Mock(return_value='✉️ already delivered')
    monkeypatch.setattr('features.mail_ops.reader_service.mail_fetch_cron', fn)
    generic_send = Mock()
    monkeypatch.setattr('services.owner_delivery.send_owner_response', generic_send)
    hosts = [SimpleNamespace(channels=[]), SimpleNamespace(channels=[])]
    apps = [create_app(mode='full', agent=h) for h in hosts]
    try:
        for app, host in zip(apps, hosts):
            app.plugin_loader.load_plugin_by_entry('features.mail_ops', 'mail_ops')
            _register_configured_cron_jobs(app, host, {'cron_jobs': [
                {'name': 'mail', 'time': '*/20 * * * *', 'skill': 'ops_mail_reader::mail_fetch_cron',
                 'kwargs': {'months': 2}, 'push_on_output': True},
            ]})
            assert run_job(app, 'mail') == '✉️ already delivered'
            assert fn.call_args.kwargs == {'agent': host, 'months': 2}
        assert fn.call_count == 2
        generic_send.assert_not_called()
    finally:
        for app in apps:
            app.shutdown()


@pytest.mark.parametrize('text,expected', [('✅ clean', False), ('⚠️ risk', True), ('[HIGH] risk', True), ('High: 1', True)])
def test_sentinel_preserves_risk_delivery_and_kwargs(monkeypatch, text, expected):
    fn = Mock(return_value=text)
    monkeypatch.setattr('features.sys_ops.security_service.sentinel_scan', fn)
    send = Mock(return_value=True)
    monkeypatch.setattr('services.owner_delivery.send_owner_response', send)
    host = SimpleNamespace(channels=[])
    app = create_app(mode='full', agent=host)
    try:
        app.plugin_loader.load_plugin_by_entry('features.sys_ops', 'sys_ops')
        _register_configured_cron_jobs(app, host, {'cron_jobs': [
            {'name': 'security', '_comment': 'real task', 'time': '04:30',
             'skill': 'ops_sentinel::sentinel_scan', 'kwargs': {'mode': 'quick'}, 'push_on_output': True},
        ]})
        assert run_job(app, 'security') == text
        fn.assert_called_once_with(mode='quick')
        assert send.call_count == int(expected)
        if expected:
            response = send.call_args.args[2]
            assert response.title == '🛡️ Sentinel 安全告警' and response.color == 'red'
    finally:
        app.shutdown()


@pytest.mark.parametrize('text,expected', [('✅ healthy', False), ('🔴 missing', True)])
def test_edge_configured_and_builtin_jobs_share_alert_wrapper(monkeypatch, text, expected):
    fn = Mock(return_value=text)
    monkeypatch.setattr('features.network_ops.edge_service.edge_health', fn)
    push = Mock(return_value=True)
    monkeypatch.setattr('core.alerts.push_alert', push)
    monkeypatch.setattr('time.time', lambda: 1200)
    host = SimpleNamespace(channels=[])
    app = create_app(mode='full', agent=host, config={'network_ops': {'failover_enabled': False}})
    try:
        app.plugin_loader.load_plugin_by_entry('features.network_ops', 'network_ops')
        assert run_job(app, 'edge_health') == text
        assert push.call_count == int(expected)
        if expected:
            assert push.call_args.args == (host, text)
            assert push.call_args.kwargs == {'title': '🛡️ Edge 失联告警', 'color': 'red', 'dedup_key': 'stale_batch:2'}
        handler = app.cron_service.get_handler('ops_edge_health::edge_health')
        builtin = next(j.func for j in app.cron_service.jobs.values() if j.name == 'edge_health')
        assert handler is builtin
        app.cron_service.remove_job_by_name('edge_health')
        _register_configured_cron_jobs(app, host, {'cron_jobs': [
            {'name': 'edge_health_sweep', 'time': '*/10 * * * *', 'skill': 'ops_edge_health::edge_health', 'push_on_output': True},
        ]})
        assert run_job(app, 'edge_health_sweep') == text
        assert push.call_count == 2 * int(expected)
    finally:
        app.shutdown()
