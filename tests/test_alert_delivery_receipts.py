"""Delivery receipts, not HTTP acceptance, control Todo completion."""
import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from channels.api import ApiHandler
from core.alerts import push_alert
from features.todo.service import TodoService, send_im_alert
from session import SessionManager


def handler_for(payload, agent):
    body = json.dumps(payload).encode()
    return SimpleNamespace(headers={'Content-Length': str(len(body))},
        rfile=io.BytesIO(body), wfile=io.BytesIO(), is_guest=False,
        server=SimpleNamespace(api_server=SimpleNamespace(agent=agent)),
        send_response=Mock(), send_header=Mock(), end_headers=Mock(),
        _send_cors_headers=Mock(), send_error=Mock())


def alert_agent(tmp_path, monkeypatch):
    channel = SimpleNamespace(name='wechat', send_to=Mock())
    monkeypatch.setattr('core.alerts.load_config', lambda: {'channels': {'wechat': {'admin_wxid': 'fixture-admin'}}})
    return SimpleNamespace(channels=[channel], session_mgr=SessionManager(str(tmp_path/'sessions.db'))), channel


def test_delivery_failure_does_not_poison_dedup_and_success_survives_restart(tmp_path, monkeypatch):
    agent, channel = alert_agent(tmp_path, monkeypatch)
    channel.send_to.side_effect = [False, True]
    assert push_alert(agent, 'notice', dedup_key='stable') is False
    assert not agent.session_mgr.has_message_processed('edge_alert:stable')
    assert push_alert(agent, 'notice', dedup_key='stable') is True
    agent.session_mgr = SessionManager(str(tmp_path/'sessions.db'))
    assert push_alert(agent, 'notice', dedup_key='stable') is True
    assert channel.send_to.call_count == 2


def test_simultaneous_same_identity_has_one_successful_send(tmp_path, monkeypatch):
    agent, channel = alert_agent(tmp_path, monkeypatch)
    channel.send_to.return_value = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: push_alert(agent, 'notice', dedup_key='same'), range(2))) == [True, True]
    assert channel.send_to.call_count == 1


@pytest.mark.parametrize('receipt', [b'{"status":"accepted"}', b'{"status":"failed"}', b'', b'[]'])
def test_todo_does_not_treat_acceptance_or_invalid_response_as_delivery(monkeypatch, receipt):
    monkeypatch.setattr('features.todo.service.urllib.request.urlopen', lambda *a, **k: io.BytesIO(receipt))
    assert send_im_alert('notice', 'text') is False


@pytest.mark.parametrize('delivered', [False, True])
def test_wait_delivery_api_returns_actual_send_outcome(monkeypatch, delivered):
    sender = Mock(return_value=delivered)
    monkeypatch.setattr('core.alerts.push_alert', sender)
    handler = handler_for({'text': 'notice', 'wait_delivery': True}, object())
    ApiHandler._handle_alert(handler)
    assert json.loads(handler.wfile.getvalue())['status'] == ('delivered' if delivered else 'failed')
    sender.assert_called_once()


def test_default_alert_keeps_ack_before_send(monkeypatch):
    handler = handler_for({'text': 'notice'}, object())
    def send(*args, **kwargs):
        assert json.loads(handler.wfile.getvalue())['status'] == 'accepted'
        return False
    monkeypatch.setattr('core.alerts.push_alert', send)
    ApiHandler._handle_alert(handler)
    assert json.loads(handler.wfile.getvalue())['status'] == 'accepted'


@pytest.mark.parametrize('kind', ['notice', 'reminder'])
def test_todo_retries_actual_failed_channel_and_records_success_once(tmp_path, monkeypatch, kind):
    agent, channel = alert_agent(tmp_path, monkeypatch)
    channel.send_to.side_effect = [False, True]
    def post(request, timeout):
        payload = json.loads(request.data)
        assert payload['wait_delivery'] is True
        handler = handler_for(payload, agent)
        ApiHandler._handle_alert(handler)
        return io.BytesIO(handler.wfile.getvalue())
    monkeypatch.setattr('features.todo.service.urllib.request.urlopen', post)
    service = TodoService(str(tmp_path/'todos.db'))
    result = service.upsert_source('billing_statement_notice:7', 'notice', 'evidence',
        created_via='billing_notice', kind=kind, due_at='2026-10-01T00:00:00+00:00',
        recur_cron='12:00' if kind == 'reminder' else None,
        source_metadata={'revision': 1, 'cycle_id': 1})
    tid = result['id']
    service.tick('2026-10-01T12:00:00+08:00')
    row = service.repo.get_todo(tid)
    assert row['status'] == 'pending' and row['notified_due_at'] is None
    service.tick('2026-10-01T12:01:00+08:00')
    completed = service.repo.get_todo(tid)
    assert completed['status'] == ('done' if kind == 'notice' else 'pending')
    assert completed['notified_due_at'] is not None
    service.tick('2026-10-01T12:02:00+08:00')
    assert channel.send_to.call_count == 2
