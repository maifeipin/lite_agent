from unittest.mock import patch
import pytest
from skills.ops_billing import billing_mark_paid


def test_paid_skill_uses_only_explicit_statement_id():
    with patch('integrations.mail.client.MailCLI') as cli:
        cli.return_value.mark_paid.return_value={'updated':True,'statement_id':7}
        assert '已标记为已还款' in billing_mark_paid(7)
        cli.return_value.mark_paid.assert_called_once_with(statement_id=7)


@pytest.mark.parametrize('value',[None,True,0,-1,'CMB'])
def test_invalid_paid_id_does_not_start_cli(value):
    with patch('integrations.mail.client.MailCLI') as cli:
        assert billing_mark_paid(value).startswith('❌')
        cli.assert_not_called()


def test_missing_statement_is_not_success():
    with patch('integrations.mail.client.MailCLI') as cli:
        cli.return_value.mark_paid.return_value={'updated':False}
        assert billing_mark_paid(7).startswith('❌')
