"""Security regressions for Telegram transport and logging."""

from pathlib import Path

from channels.telegram import TelegramChannel


class _Response:
    def __init__(self, payload=None, status_code=200, content=b""):
        self._payload = payload
        self.status_code = status_code
        self.content = content

    def json(self):
        return self._payload


class _Client:
    def __init__(self, response=None, error=None):
        self.response = response or _Response({"ok": True})
        self.error = error
        self.post_calls = []
        self.closed = False

    def post(self, url, json):
        self.post_calls.append((url, json))
        if self.error:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


def _channel(client, proxy="socks5h://proxy-user:proxy-pass@127.0.0.1:18988"):
    return TelegramChannel(
        {"bot_token": "unit-test-token", "proxy": proxy},
        agent=object(),
        http_client=client,
    )


def test_transport_has_no_subprocess_or_curl_invocation():
    source = Path(__file__).parents[1].joinpath("channels", "telegram.py").read_text(
        encoding="utf-8"
    )
    assert "subprocess" not in source
    assert "curl" not in source.lower()


def test_bot_api_uses_in_process_http_client():
    client = _Client(_Response({"ok": True, "result": []}))
    channel = _channel(client)

    result = channel._request("getUpdates", {"offset": 42})

    assert result == {"ok": True, "result": []}
    assert client.post_calls == [
        (
            "https://api.telegram.org/botunit-test-token/getUpdates",
            {"offset": 42},
        )
    ]


def test_request_error_log_does_not_disclose_credentials(capsys):
    error = RuntimeError(
        "failed https://api.telegram.org/botunit-test-token/getUpdates "
        "via proxy-user:proxy-pass"
    )
    channel = _channel(_Client(error=error))

    assert channel._request("getUpdates") == {}

    output = capsys.readouterr().out
    assert "RuntimeError" in output
    assert "unit-test-token" not in output
    assert "proxy-user" not in output
    assert "proxy-pass" not in output


def test_proxy_label_redacts_userinfo():
    channel = _channel(_Client())

    label = channel._safe_proxy_label()

    assert label == "socks5h://127.0.0.1:18988"
    assert "proxy-user" not in label
    assert "proxy-pass" not in label


def test_injected_http_client_is_not_closed_by_channel():
    client = _Client()
    channel = _channel(client)

    channel.stop()

    assert client.closed is False
