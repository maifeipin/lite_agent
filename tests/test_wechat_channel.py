"""Offline integration tests for WeChatChannel's Phase 1 state machine."""

import json

import pytest

import channels.wechat as wechat_module
from agent import AgentResponse
from channels.wechat_ilink import ILinkAuthError, ILinkContextError


class FakeSessionManager:
    def __init__(self):
        self.seen = set()

    def is_message_processed(self, message_id):
        if message_id in self.seen:
            return True
        self.seen.add(message_id)
        return False


class FakeAgent:
    def __init__(self):
        self.session_mgr = FakeSessionManager()
        self.received = []

    def handle(self, message):
        self.received.append(message)
        return AgentResponse("done")


class InlineExecutor:
    def submit(self, fn, *args):
        return fn(*args)

    def shutdown(self, wait=False):
        return None


class FakeClient:
    def __init__(self, **kwargs):
        self.sent = []
        self.logged_out = False
        self.cursor_cleared = False
        self.context_error = False

    def load_credential(self):
        return True

    def send_text(self, talker, token, text):
        if self.context_error:
            raise ILinkContextError("context expired")
        self.sent.append((talker, token, text))
        return True

    def logout_cleanup(self):
        self.logged_out = True

    def clear_cursor(self):
        self.cursor_cleared = True

    def close(self):
        return None


@pytest.fixture
def channel(tmp_path, monkeypatch):
    monkeypatch.setattr(wechat_module, "ILinkClient", FakeClient)
    instance = wechat_module.WeChatChannel({
        "admin_wxid": "wxid_admin",
        "session_file": str(tmp_path / "session.json"),
        "contexts_file": str(tmp_path / "contexts.json"),
        "cursor_file": str(tmp_path / "cursor.json"),
        "context_ttl_hours": 24,
        "context_max_sends": 2,
        "max_msg_len": 6,
    }, FakeAgent())
    instance.executor = InlineExecutor()
    return instance


def test_context_counter_only_resets_when_token_changes(channel, tmp_path):
    channel._remember_context("wxid_admin", "token-a")
    channel._consume_context("wxid_admin")
    channel._remember_context("wxid_admin", "token-a")
    assert channel._contexts["wxid_admin"]["sends_used"] == 1

    channel._remember_context("wxid_admin", "token-b")
    assert channel._contexts["wxid_admin"]["sends_used"] == 0
    stored = json.loads((tmp_path / "contexts.json").read_text(encoding="utf-8"))
    assert stored["wxid_admin"]["context_token"] == "token-b"


def test_send_requires_valid_context_and_enforces_per_token_quota(channel):
    assert channel.send_to("wxid_admin", AgentResponse("none")) is False
    channel._remember_context("wxid_admin", "token-a")
    assert channel.send_to("wxid_admin", AgentResponse("first")) is True
    assert channel.send_to("wxid_admin", AgentResponse("second")) is True
    assert channel.send_to("wxid_admin", AgentResponse("third")) is False
    assert [item[2] for item in channel.client.sent] == ["first", "second"]


def test_context_error_invalidates_only_that_user(channel):
    channel._remember_context("wxid_admin", "token-a")
    channel._remember_context("wxid_other", "token-b")
    channel.client.context_error = True
    assert channel.send_to("wxid_admin", AgentResponse("hello")) is False
    assert channel._valid_token("wxid_admin") is None
    assert channel._valid_token("wxid_other") == "token-b"


def test_stale_push_context_error_cannot_invalidate_newer_token(channel):
    channel._remember_context("wxid_admin", "old-token")
    channel._remember_context("wxid_admin", "new-token")
    channel.client.context_error = True
    assert channel._send_segmented("wxid_admin", "old-token", "hello") is False
    assert channel._valid_token("wxid_admin") == "new-token"


def test_update_maps_message_and_marks_non_admin_as_guest(channel):
    raw = {
        "message_id": 101,
        "from_user_id": "wxid_guest_with_underscore",
        "to_user_id": "bot",
        "message_type": 1,
        "message_state": 2,
        "context_token": "context-token",
        "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
    }
    channel._handle_update(raw)
    assert len(channel.agent.received) == 1
    incoming = channel.agent.received[0]
    assert incoming.channel == "wechat"
    assert incoming.user_id == "wxid_guest_with_underscore"
    assert incoming.is_guest is True
    assert incoming.channel_payload == {"talker": "wxid_guest_with_underscore", "context_token": "context-token"}
    assert channel._parse_talker(incoming.message_id) == "wxid_guest_with_underscore"
    channel._handle_update(raw)
    assert len(channel.agent.received) == 1


def test_utf8_segments_do_not_split_a_character(channel):
    assert channel._split_text("你a好b") == ["你a", "好b"]
    assert all(len(segment.encode("utf-8")) <= 6 for segment in channel._split_text("你a好b"))


def test_minus_14_keeps_first_credential_then_clears_on_second(channel):
    waits = []
    channel._wait = lambda seconds: waits.append(seconds)
    error = ILinkAuthError("stale", permanent=False)
    channel._handle_auth_error(error)
    assert channel.client.logged_out is False
    assert channel.client.cursor_cleared is True
    assert channel._stale_probe_pending is True
    assert channel._isolate_until > 0

    channel._handle_auth_error(error)
    assert channel.client.logged_out is True
    assert waits == [60]


def test_permanent_auth_error_clears_credential(channel):
    waits = []
    channel._wait = lambda seconds: waits.append(seconds)
    channel._handle_auth_error(ILinkAuthError("permanent", permanent=True))
    assert channel.client.logged_out is True
    assert waits == [60]
