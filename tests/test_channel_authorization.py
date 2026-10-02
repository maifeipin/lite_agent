import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

from channels.api import ApiHandler
from channels.telegram import TelegramChannel
from channels.wecom import _make_handler


def test_api_guest_identity_and_notifications_are_not_self_reported():
    handler = object.__new__(ApiHandler)
    handler.is_guest = True
    body = json.dumps({'session_id': 'test', 'text': 'hello',
                       'notify_channels': ['feishu'], 'is_guest': False}).encode()
    handler.headers = {'Content-Length': str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    agent = Mock()
    agent.handle.return_value = None
    handler.server = SimpleNamespace(api_server=SimpleNamespace(agent=agent))
    handler.send_response = Mock()
    handler._send_cors_headers = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler._handle_chat_or_task()
    message = agent.handle.call_args.args[0]
    assert message.is_guest is True
    assert message.notify_channels == ['api']
    assert message.user_id == 'guest/test'


def test_telegram_owner_requires_private_sender_identity():
    channel = object.__new__(TelegramChannel)
    channel.config = {'admin_chat_id': '123'}
    assert channel._is_owner_message({'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123}})
    assert not channel._is_owner_message({'chat': {'id': 123, 'type': 'group'}, 'from': {'id': 456}})
    assert not channel._is_owner_message({'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 456}})
    assert not channel._is_owner_message({'chat': {'type': 'private'}})
    channel.config = {'admin_chat_id': '-123'}
    assert not channel._is_owner_message({'chat': {'type': 'private'}, 'from': {'id': -123}})


def test_wecom_unauthenticated_bridge_cannot_spoof_owner():
    channel = SimpleNamespace(bridge_secret='test-secret', executor=Mock())
    handler = object.__new__(_make_handler(channel))
    handler.headers = {'Content-Length': '100', 'X-Bridge-Secret': 'wrong'}
    handler.send_error = Mock()
    handler.rfile = Mock()
    handler.do_POST()
    handler.send_error.assert_called_once_with(401, 'Unauthorized')
    handler.rfile.read.assert_not_called()
    channel.executor.submit.assert_not_called()


def test_api_missing_secrets_fails_closed():
    handler = object.__new__(ApiHandler)
    handler.server = SimpleNamespace(api_server=SimpleNamespace(auth_token='', config={}))
    handler.send_error = Mock()
    from unittest.mock import patch
    with patch.dict('os.environ', {'EDGE_TOKEN': ''}):
        assert handler._auth() is False
    assert handler.send_error.call_args.args[0] == 503


def test_guest_cannot_read_admin_history_or_modify_todos():
    for method, path in [('do_GET', '/api/v1/sessions/messages?session_key=feishu:owner'),
                         ('do_POST', '/api/v1/socks5'),
                         ('do_PATCH', '/api/v1/todos/123'),
                         ('do_DELETE', '/api/v1/todos/123')]:
        handler = object.__new__(ApiHandler)
        handler.path = path
        handler.is_guest = True
        handler.is_edge = False
        handler._auth = Mock(return_value=True)
        handler.send_error = Mock()
        getattr(handler, method)()
        assert handler.send_error.call_args.args[0] == 403


def test_edge_cannot_modify_admin_resources_after_authentication():
    handler = object.__new__(ApiHandler)
    handler.path = '/api/v1/todos/123'
    handler.is_edge = False
    handler.is_guest = False
    def authenticate():
        handler.is_edge = True
        return True
    handler._auth = authenticate
    handler.send_error = Mock()
    handler.do_PATCH()
    assert handler.send_error.call_args.args[0] == 403
