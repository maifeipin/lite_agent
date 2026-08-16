"""Fixture-driven contract tests for the Phase 0B iLink protocol client."""

import base64
import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from channels.wechat_ilink import (
    ILinkAuthError,
    ILinkClient,
    ILinkContextError,
)


FIXTURES = Path(__file__).with_name("fixtures")


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _credential(client, root):
    client._save_session({
        "bot_token": "test-token",
        "ilink_bot_id": "bot-fixture",
        "baseurl": "https://ilinkai.weixin.qq.com",
        "ilink_user_id": "wxid_bot",
        "unexpected_secret": "must-not-be-persisted",
    })
    assert client.load_credential()
    written = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert set(written) == {"token", "bot_id", "base_url", "user_id", "saved_at"}


def _client(tmp_path, handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = ILinkClient(
        session_file=str(tmp_path / "session.json"),
        cursor_file=str(tmp_path / "cursor.json"),
        http_client=http,
    )
    _credential(client, tmp_path)
    return client


def test_sendmessage_golden_body_headers_and_octet_stream_response(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/octet-stream"},
            content=json.dumps(_fixture("sendmessage_success_octet_stream.json")).encode("utf-8"),
        )

    client = _client(tmp_path, handler)
    assert client.send_text("wxid_target", "context-token", "hello iLink") is True

    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/ilink/bot/sendmessage"
    assert request.headers["AuthorizationType"] == "ilink_bot_token"
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.headers["iLink-App-Id"] == "bot"
    assert request.headers["iLink-App-ClientVersion"] == "132102"
    assert int(base64.b64decode(request.headers["X-WECHAT-UIN"]).decode("ascii")) >= 0

    body = json.loads(request.content)
    assert set(body) == {"msg", "base_info"}
    assert set(body["base_info"]) == {"channel_version", "bot_agent"}
    assert body["base_info"]["channel_version"] == "2.4.6"
    assert body["base_info"]["bot_agent"].isascii()
    assert set(body["msg"]) == {
        "from_user_id", "to_user_id", "client_id", "message_type",
        "message_state", "item_list", "context_token",
    }
    assert body["msg"]["from_user_id"] == ""
    assert body["msg"]["to_user_id"] == "wxid_target"
    assert body["msg"]["client_id"].startswith("lite_agent-wechat-")
    assert body["msg"]["message_type"] == 2
    assert body["msg"]["message_state"] == 2
    assert body["msg"]["item_list"] == [{"type": 1, "text_item": {"text": "hello iLink"}}]
    assert body["msg"]["context_token"] == "context-token"


def test_every_business_request_regenerates_uin(tmp_path):
    uins = []

    def handler(request):
        uins.append(request.headers["X-WECHAT-UIN"])
        if request.url.path.endswith("getupdates"):
            return httpx.Response(200, json={"ret": 0, "msgs": []})
        return httpx.Response(200, json={"ret": 0})

    client = _client(tmp_path, handler)
    client.get_updates()
    client.send_text("wxid_target", "token", "first")
    assert len(uins) == 2
    assert uins[0] != uins[1]


def test_every_message_uses_a_distinct_client_id(tmp_path):
    client_ids = []

    def handler(request):
        client_ids.append(json.loads(request.content)["msg"]["client_id"])
        return httpx.Response(200, json={"ret": 0})

    client = _client(tmp_path, handler)
    client.send_text("wxid_target", "token", "first")
    client.send_text("wxid_target", "token", "second")
    assert len(client_ids) == 2
    assert client_ids[0] != client_ids[1]


def test_non_auth_http_error_and_malformed_success_are_not_treated_as_sent(tmp_path):
    responses = iter([
        httpx.Response(429, json={"errmsg": "rate limited"}),
        httpx.Response(200, content=b"not-json"),
    ])

    def handler(_request):
        return next(responses)

    client = _client(tmp_path, handler)
    assert client.send_text("wxid_target", "token", "first") is False
    assert client.send_text("wxid_target", "token", "second") is False


def test_qr_login_uses_post_body_but_only_displays_image_url(tmp_path):
    seen = []
    displayed = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("get_bot_qrcode"):
            return httpx.Response(200, json={
                "qrcode": "opaque-query-key",
                "qrcode_img_content": "https://liteapp.weixin.qq.com/q/fixture",
            })
        return httpx.Response(200, json={
            "status": "confirmed",
            "bot_token": "test-token",
            "ilink_bot_id": "bot-fixture",
            "baseurl": "https://ilinkai.weixin.qq.com",
            "ilink_user_id": "wxid_bot",
        })

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = ILinkClient(
        session_file=str(tmp_path / "session.json"),
        cursor_file=str(tmp_path / "cursor.json"),
        http_client=http,
    )
    client.ensure_login_cli(displayed.append, on_status=lambda _status: None)

    qr_request, status_request = seen
    assert qr_request.method == "POST"
    assert qr_request.url.path == "/ilink/bot/get_bot_qrcode"
    assert qr_request.url.params["bot_type"] == "3"
    assert json.loads(qr_request.content) == {"local_token_list": []}
    assert displayed == ["https://liteapp.weixin.qq.com/q/fixture"]
    assert status_request.url.params["qrcode"] == "opaque-query-key"
    assert "qrcode_img_content" not in str(status_request.url)
    assert client.load_credential() is True


def test_getupdates_persists_and_reuses_opaque_cursor(tmp_path):
    sent_cursors = []

    def first_handler(request):
        sent_cursors.append(json.loads(request.content)["get_updates_buf"])
        return httpx.Response(200, json=_fixture("getupdates_success.json"))

    first = _client(tmp_path, first_handler)
    assert first.get_updates()[0]["context_token"] == "context-fixture-token"
    first.close()

    def second_handler(request):
        sent_cursors.append(json.loads(request.content)["get_updates_buf"])
        return httpx.Response(200, json={"ret": 0, "msgs": []})

    second = _client(tmp_path, second_handler)
    assert second.get_updates() == []
    assert sent_cursors == ["", "opaque-cursor-v2"]
    assert json.loads((tmp_path / "cursor.json").read_text(encoding="utf-8")) == {
        "get_updates_buf": "opaque-cursor-v2"
    }


def test_getupdates_minus_14_clears_only_cursor(tmp_path):
    def handler(_request):
        return httpx.Response(200, json={"ret": -14})

    client = _client(tmp_path, handler)
    (tmp_path / "cursor.json").write_text('{"get_updates_buf":"old"}', encoding="utf-8")
    client._cursor = "old"
    with pytest.raises(ILinkAuthError) as raised:
        client.get_updates()
    assert raised.value.permanent is False
    assert not (tmp_path / "cursor.json").exists()
    assert (tmp_path / "session.json").exists()


def test_sendmessage_minus_2_is_context_error_and_keeps_credential(tmp_path):
    def handler(_request):
        return httpx.Response(200, json={"ret": -2})

    client = _client(tmp_path, handler)
    with pytest.raises(ILinkContextError):
        client.send_text("wxid_target", "context-token", "message")
    assert (tmp_path / "session.json").exists()


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_failure_is_permanent(tmp_path, status):
    def handler(_request):
        return httpx.Response(status)

    client = _client(tmp_path, handler)
    with pytest.raises(ILinkAuthError) as raised:
        client.get_updates()
    assert raised.value.permanent is True


def test_runtime_files_are_private_on_posix(tmp_path):
    def handler(_request):
        return httpx.Response(200, json={"ret": 0, "get_updates_buf": "cursor", "msgs": []})

    client = _client(tmp_path, handler)
    client.get_updates()
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "session.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "cursor.json").stat().st_mode) == 0o600
