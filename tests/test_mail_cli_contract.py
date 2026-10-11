from unittest.mock import Mock

import pytest

from integrations.mail.client import MailCLI


@pytest.mark.parametrize("statement_id", [None, 0, -1, True, "1", "bad"])
def test_invalid_paid_identity_never_executes_cli(statement_id):
    client = MailCLI(script_dir="/fixture")
    client.run = Mock()
    with pytest.raises(ValueError):
        client.mark_paid(statement_id=statement_id)
    client.run.assert_not_called()


def test_legacy_paid_identity_never_executes_cli():
    client = MailCLI(script_dir="/fixture")
    client.run = Mock()
    with pytest.raises(ValueError):
        client.mark_paid(bank_code="CMB", statement_month="2026-10")
    client.run.assert_not_called()


def test_exact_paid_identity_uses_json_command():
    client = MailCLI(script_dir="/fixture")
    client.run = Mock(return_value={"status": "ok", "data": {"statement_id": 7, "updated": True}})
    assert client.mark_paid(statement_id=7) == {"statement_id": 7, "updated": True}
    client.run.assert_called_once_with("mark_paid", ["--statement-id", "7"])


def test_identity_preview_argument_validation_and_call():
    client = MailCLI(script_dir="/fixture")
    client.run = Mock(return_value={"status": "ok", "data": {"plan_id": "p1", "status": "associated_existing"}})

    with pytest.raises(ValueError):
        client.identity_preview(statement_id=-1, account="a", transport_uid="u", bank="CMB")

    res = client.identity_preview(
        statement_id=10, account="my_acct", transport_uid="u100", bank="CMB", eml_path="/p/file.eml"
    )
    assert res == {"plan_id": "p1", "status": "associated_existing"}
    client.run.assert_called_once_with("identity_preview", [
        "--statement-id", "10",
        "--account", "my_acct",
        "--transport-kind", "pop3_uidl",
        "--transport-uid", "u100",
        "--bank", "CMB",
        "--eml", "/p/file.eml"
    ])


def test_identity_confirm_argument_validation_and_call():
    client = MailCLI(script_dir="/fixture")
    client.run = Mock(return_value={"status": "ok", "data": {"confirmed": True}})

    with pytest.raises(ValueError):
        client.identity_confirm(plan_path="/p.json", plan_hash="h", actor="", reason="r")

    with pytest.raises(ValueError):
        client.identity_confirm(plan_path="/p.json", plan_hash="h", actor="alice", reason="   ")

    res = client.identity_confirm(plan_path="/p.json", plan_hash="h123", actor="alice", reason="verified")
    assert res == {"confirmed": True}
    client.run.assert_called_once_with("identity_confirm", [
        "--plan", "/p.json",
        "--plan-hash", "h123",
        "--actor", "alice",
        "--reason", "verified"
    ])


def test_identity_status_call():
    client = MailCLI(script_dir="/fixture")
    client.run = Mock(return_value={"status": "ok", "data": {"identities": []}})

    res = client.identity_status(statement_id=10, account="acct")
    assert res == {"identities": []}
    client.run.assert_called_once_with("identity_status", [
        "--statement-id", "10",
        "--account", "acct"
    ])


def test_sync_receipt_accepts_identity_counters():
    from integrations.mail.sync_receipt import parse_json_summary
    valid_payload = """--- JSON_SUMMARY_START ---
    {
        "accounts": [
            {
                "account": "user@163.com",
                "status": "ok",
                "scanned": 10,
                "new_bills": 1,
                "associated_existing": 2,
                "identity_conflict": 0,
                "insufficient_evidence": 0
            }
        ]
    }
    --- JSON_SUMMARY_END ---"""
    summary = parse_json_summary(valid_payload)
    assert summary is not None
    assert summary["accounts"][0]["associated_existing"] == 2

    # 反例：负数计数必须被拒绝
    invalid_payload = """--- JSON_SUMMARY_START ---
    {
        "accounts": [
            {
                "account": "user@163.com",
                "status": "ok",
                "associated_existing": -1
            }
        ]
    }
    --- JSON_SUMMARY_END ---"""
    assert parse_json_summary(invalid_payload) is None
