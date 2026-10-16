from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.output_delivery import (
    _markdown_to_html,
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
    assert "仅登录账号可见" in result


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
    assert "完整回复已提交邮件通道" in result
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


def test_explicit_sqlite_is_reported_as_requested_not_external_failure(tmp_path):
    config = {
        "output_delivery": {"sqlite": {"path": str(tmp_path / "archive.db")}},
    }

    result = prepare_channel_output(
        "完整结果", "wechat", config,
        overrides={"full_delivery": "sqlite"},
    )

    assert "已按任务设置" in result
    assert "外部发送均不可用" not in result


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
    (tmp_path / "email-config.local.json").write_text("{}", encoding="utf-8")
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

    observed_candidates = []

    def load_accounts():
        observed_candidates.extend(mail_client.CONFIG_CANDIDATES)
        assert all(path.startswith(str(tmp_path)) for path in observed_candidates)
        return accounts

    mail_client = SimpleNamespace(
        CONFIG_CANDIDATES=["email-config.local.json", "email-config.json"],
        load_accounts=load_accounts,
    )
    mail_connect = SimpleNamespace(
        is_graph_api=lambda _account: False,
        connect_smtp=connect,
        send_email_graph=MagicMock(),
    )
    smtp = MagicMock()
    config = {
        "output_delivery": {
            "email": {
                "enabled": True,
                "recipient": "owner@example.com",
                "accounts": [
                    {"provider": "qq", "host": "smtp.qq.com", "port": 465, "username": "qq@example.com", "password": "p1"},
                    {"provider": "163", "host": "smtp.163.com", "port": 465, "username": "n@example.com", "password": "p2"},
                ],
            }
        }
    }

    # 第一个账号 qq 模拟连接失败，第二个账号 163 模拟成功
    attempted = []

    def fake_sender(client):
        attempted.append(client.username)
        if "qq" in client.username:
            raise ConnectionError("QQ 连接失败模拟")
        return smtp

    with patch("integrations.smtp.client.smtplib.SMTP_SSL", side_effect=lambda h, p, **kw: fake_sender(MagicMock(username=h))):
        provider = _send_email("full text", config, title="report")

    assert provider == "163"
    smtp.send_message.assert_called_once()


def test_send_email_attaches_html_alternative():
    """验证标准 SMTP 投递生成正确的 multipart/alternative 格式。"""
    sent_messages = []
    fake_smtp = MagicMock()
    fake_smtp.send_message.side_effect = lambda msg: sent_messages.append(msg)

    config = {
        "output_delivery": {
            "email": {
                "enabled": True,
                "recipient": "owner@example.com",
                "host": "smtp.example.com",
                "port": 465,
                "username": "bot@example.com",
                "password": "pwd",
            }
        }
    }

    with patch("integrations.smtp.client.smtplib.SMTP_SSL", return_value=fake_smtp):
        provider = _send_email("# 报告\n\n- 项目", config, title="t")

    assert provider == "output_email"
    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert msg.get_content_type() == "multipart/alternative"
    parts = {p.get_content_type() for p in msg.walk() if not p.is_multipart()}
    assert parts == {"text/plain", "text/html"}


def test_send_email_decoupled_parser_path_absent_still_delivers(tmp_path):
    """验证 Parser 路径完全不存在或被破坏时，标准 SMTP 仍能成功投递。"""
    non_existent = str(tmp_path / "non_existent_parser_dir")
    sent_messages = []
    fake_smtp = MagicMock()
    fake_smtp.send_message.side_effect = lambda msg: sent_messages.append(msg)

    config = {
        "billing": {"script_dir": non_existent},
        "output_delivery": {
            "email": {
                "enabled": True,
                "recipient": "owner@example.com",
                "host": "smtp.independent.com",
                "port": 465,
                "username": "independent@example.com",
                "password": "pwd",
            }
        }
    }

    with patch("integrations.smtp.client.smtplib.SMTP_SSL", return_value=fake_smtp):
        provider = _send_email("正文", config, title="独立投递测试")

    assert provider == "output_email"
    assert len(sent_messages) == 1


def test_send_email_failure_does_not_fake_success():
    """验证 SMTP 发送失败时返回空字符串，不可假装成功。"""
    config = {
        "output_delivery": {
            "email": {
                "enabled": True,
                "recipient": "owner@example.com",
                "host": "smtp.fail.com",
                "port": 465,
                "username": "user@fail.com",
                "password": "pwd",
            }
        }
    }
    with patch("integrations.smtp.client.smtplib.SMTP_SSL", side_effect=ConnectionError("网络不可达")):
        provider = _send_email("正文", config, title="失败测试")
    assert provider == ""


def test_markdown_to_html_renders_structures_and_escapes():
    md = (
        "# 标题\n\n**重点** <script>\n\n"
        "- 第一项\n- 第二项\n\n"
        "| 币种 | 金额 |\n| --- | --- |\n| CNY | 1245.95 |\n"
    )
    out = _markdown_to_html(md)
    assert "<h1>标题</h1>" in out
    assert "<strong>重点</strong>" in out
    assert "&lt;script&gt;" in out and "<script>" not in out
    assert out.count("<li>") == 2
    assert "<th>币种</th>" in out and "<td>1245.95</td>" in out
    assert out.startswith("<html>")


def test_markdown_to_html_table_requires_separator_row():
    out = _markdown_to_html("| 不是表格\n| 没有分隔行 |")
    assert "<table>" not in out
    assert "| 不是表格" in out


def test_markdown_to_html_escaped_pipe_stays_in_one_cell():
    """R5: 报告生成方转义的 \\| 不得被当成列界，否则金额错列。"""
    md = "| 商户 | 币种 | 金额 |\n| --- | --- | --- |\n| A\\|B | CNY | 10.00 |"
    out = _markdown_to_html(md)
    assert out.count("<td>") == 3
    assert "<td>A|B</td>" in out
    assert "<td>CNY</td>" in out and "<td>10.00</td>" in out


def test_markdown_to_html_misaligned_table_degrades_to_pre():
    out = _markdown_to_html("| a | b |\n| --- | --- |\n| 1 | 2 | 3 |")
    assert "<table>" not in out
    assert "<pre>" in out and "1 | 2 | 3" in out


def test_markdown_to_html_list_before_adjacent_paragraph_keeps_order():
    out = _markdown_to_html("- 第一项\n说明段落")
    assert "<li>第一项</li>" in out
    assert out.index("<ul>") < out.index("<p>说明段落</p>")


def test_smtp_port_defaults_and_env_overrides():
    import os
    from unittest.mock import patch
    from integrations.smtp.client import _extract_smtp_accounts, send_email_delivery

    # 1. 验证 587 端口默认 STARTTLS (use_ssl=False, use_tls=True)
    cfg_587 = {
        "smtp": {
            "enabled": True,
            "host": "smtp.office365.com",
            "port": 587,
            "username": "user@example.com",
            "password": "pwd",
        }
    }
    accs_587 = _extract_smtp_accounts(cfg_587)
    assert len(accs_587) == 1
    assert accs_587[0]["use_ssl"] is False
    assert accs_587[0]["use_tls"] is True

    # 2. 验证 465 端口默认 SSL (use_ssl=True, use_tls=False)
    cfg_465 = {
        "smtp": {
            "enabled": True,
            "host": "smtp.qq.com",
            "port": 465,
            "username": "user@qq.com",
            "password": "pwd",
        }
    }
    accs_465 = _extract_smtp_accounts(cfg_465)
    assert len(accs_465) == 1
    assert accs_465[0]["use_ssl"] is True
    assert accs_465[0]["use_tls"] is False

    # 3. 验证环境变量覆盖
    env = {
        "LITE_AGENT_SMTP_HOST": "smtp.env.com",
        "LITE_AGENT_SMTP_PORT": "587",
        "LITE_AGENT_SMTP_USER": "env_user@env.com",
        "LITE_AGENT_SMTP_PASSWORD": "env_pwd",
        "LITE_AGENT_SMTP_USE_SSL": "false",
        "LITE_AGENT_EMAIL_PREFIX": "[ENV PREFIX]",
        "LITE_AGENT_SMTP_RECIPIENT": "recipient@env.com",
        "LITE_AGENT_SMTP_ENABLED": "true",
    }
    with patch.dict(os.environ, env):
        env_accs = _extract_smtp_accounts({})
        assert len(env_accs) >= 1
        assert env_accs[0]["provider"] == "env_smtp"
        assert env_accs[0]["host"] == "smtp.env.com"
        assert env_accs[0]["use_ssl"] is False
        assert env_accs[0]["use_tls"] is True

        fake_smtp = MagicMock()
        prov = send_email_delivery("正文内容", {}, title="测试", sender_factory=lambda c: fake_smtp)
        assert prov == "env_smtp"
        fake_smtp.send_message.assert_called_once()
        sent_msg = fake_smtp.send_message.call_args[0][0]
        assert "[ENV PREFIX] 测试" in sent_msg["Subject"]


def test_smtp_provider_order_preference():
    from integrations.smtp.client import _extract_smtp_accounts

    cfg = {
        "smtp": {
            "accounts": [
                {"provider": "p1", "host": "h1.com", "username": "u1"},
                {"provider": "p2", "host": "h2.com", "username": "u2"},
                {"provider": "p3", "host": "h3.com", "username": "u3"},
            ],
            "provider_order": ["p3", "p1", "p2"],
        }
    }
    ordered = _extract_smtp_accounts(cfg)
    providers = [a["provider"] for a in ordered]
    assert providers == ["p3", "p1", "p2"]
