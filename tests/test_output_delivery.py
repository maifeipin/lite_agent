from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.output_delivery import (
    _send_email,
    get_archived_output,
    prepare_channel_output,
    resolve_output_policy,
)


def test_default_policy_is_summary_with_reliable_fallbacks():
    policy = resolve_output_policy({})
    assert policy["full_delivery"] == "auto"
    assert policy["reply_mode"] == "summary"
    assert policy["fallback_order"] == ["hedgedoc", "email", "sqlite"]
    assert policy["explicit"] is False


def test_api_output_is_inline():
    text = "x" * 5000
    assert prepare_channel_output(text, "api", {}) == text


def test_long_output_uses_public_hedgedoc_when_available():
    config = {
        "hedgedoc": {"enabled": True},
        "output_delivery": {"channel_limits": {"wecom": 100}},
    }
    with patch("core.utils.hedgedoc.upload_to_hedgedoc", return_value="https://md/report"):
        result = prepare_channel_output("x" * 300, "wecom", config)

    assert len(result) <= 100
    assert "https://md/report" in result
    assert "公开链接" in result


def test_hedgedoc_and_email_failure_falls_back_to_sqlite(tmp_path):
    config = {
        "hedgedoc": {"enabled": True},
        "output_delivery": {
            "channel_limits": {"dingtalk": 160},
            "sqlite": {"path": str(tmp_path / "archive.db")},
        },
    }
    with patch("core.utils.hedgedoc.upload_to_hedgedoc", return_value=""), \
            patch("core.output_delivery._send_email", return_value=""):
        result = prepare_channel_output("x" * 300, "dingtalk", config)

    assert len(result) <= 160
    archive_id = result.split("归档 ID：", 1)[1].split("）", 1)[0]
    assert get_archived_output(archive_id, config)["content"] == "x" * 300


def test_explicit_email_delivers_even_short_reply():
    config = {"output_delivery": {"channel_limits": {"wechat": 2000}}}
    with patch("core.output_delivery._send_email", return_value="qq") as send:
        result = prepare_channel_output(
            "完整答案", "wechat", config,
            overrides={"full_delivery": "email"},
        )
    send.assert_called_once()
    assert "完整回复已发送" in result
    assert "qq" in result


def test_explicit_email_failure_does_not_publish_to_hedgedoc(tmp_path):
    config = {
        "hedgedoc": {"enabled": True},
        "output_delivery": {"sqlite": {"path": str(tmp_path / "archive.db")}},
    }
    with patch("core.output_delivery._send_email", return_value=""), \
            patch("core.utils.hedgedoc.upload_to_hedgedoc") as hedgedoc:
        result = prepare_channel_output(
            "私密回复", "wechat", config,
            overrides={"full_delivery": "email"},
        )
    hedgedoc.assert_not_called()
    assert "归档 ID" in result


def test_summary_callback_is_used_before_link():
    config = {
        "hedgedoc": {"enabled": True},
        "output_delivery": {"channel_limits": {"wechat": 120}},
    }
    with patch("core.utils.hedgedoc.upload_to_hedgedoc", return_value="https://md/r"):
        result = prepare_channel_output(
            "原文" * 200, "wechat", config,
            summarize=lambda _text, _limit: "这是摘要。",
        )
    assert result.startswith("这是摘要。")
    assert "https://md/r" in result


def test_email_provider_order_reuses_ops_mail_accounts(tmp_path):
    (tmp_path / "mail_client.py").write_text("# marker", encoding="utf-8")
    accounts = [
        {"provider": "gmail", "account": "g@example"},
        {"provider": "163", "account": "n@example"},
        {"provider": "qq", "account": "q@example"},
        {"provider": "outlook", "account": "o@example"},
    ]
    attempted = []
    smtp = MagicMock()

    def connect(account):
        attempted.append(account["provider"])
        if account["provider"] == "qq":
            raise OSError("qq unavailable")
        return smtp

    mail_client = SimpleNamespace(load_accounts=lambda: accounts)
    mail_connect = SimpleNamespace(
        is_graph_api=lambda _account: False,
        connect_smtp=connect,
        send_email_graph=MagicMock(),
    )
    config = {"output_delivery": {"email": {
        "enabled": True, "recipient": "owner@example",
        "script_dir": str(tmp_path),
        "provider_order": ["qq", "163", "outlook", "gmail"],
    }}}

    with patch("core.output_delivery.importlib.import_module") as load:
        load.side_effect = lambda name: mail_client if name == "mail_client" else mail_connect
        provider = _send_email("full text", config, title="report")

    assert provider == "163"
    assert attempted == ["qq", "163"]
    smtp.send_message.assert_called_once()
