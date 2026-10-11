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
