"""The production IM case: proper tools, scoped sync, request-local reuse."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.agent_runtime import AgentRuntime, RuntimeEventType
from core.billing_workflow import BillingWorkflow
from integrations.mail.client import MailCLI
from features.billing.sync_service import sync_and_consume
from tests.test_agent_runtime import _make_ctx, _make_sync_result
from tests.test_request_selector import StubEngine
from core.request_selector import RequestSelector
from core.skill_engine import SkillEngine, skill


def test_original_im_text_selects_billing_and_foreign_currency_evidence():
    engine = StubEngine()
    result = RequestSelector(engine).select('再拉取一下最新的帐单,分析外币和异常金额并总结到邮箱')
    assert 'billing' in result.domains
    # A cross-domain request may deliberately fall back to all registered tools.
    names = result.names if result.names is not None else engine.get_all_names()
    assert 'billing_fetch' in names
    assert 'billing_snapshot' in names


def test_workflow_recognizes_common_billing_spelling():
    workflow = BillingWorkflow({'task_routing': {'billing_workflow': {'enabled': True}}}, MagicMock())
    assert workflow.matches('拉取最新帐单，分析后总结到邮箱')
    assert not workflow.matches('拉取最新帐单，分析并标记已还款')


def test_foreign_currency_query_exposes_read_only_snapshot():
    result = RequestSelector(StubEngine()).select('分析外币和原币金额')
    assert result.domains == ['billing']
    assert 'billing_snapshot' in result.names
    assert 'billing_fetch' not in result.names
    assert result.read_only_mode


@pytest.mark.parametrize('account,provider,expected', [
    (None, '163', ['--provider', '163']),
    ('explicit@example.invalid', '163', ['--account', 'explicit@example.invalid']),
])
def test_mail_cli_passes_scope_without_broadening(account, provider, expected, tmp_path):
    with patch.object(MailCLI, '_check_security', return_value=None):
        runner = MagicMock(return_value=SimpleNamespace(returncode=0, stdout='', stderr=''))
        MailCLI(str(tmp_path)).sync_fetch(runner=runner, account=account, provider=provider)
    assert runner.call_args.args[0][-2:] == expected


def test_billing_service_defaults_to_163_but_honors_explicit_account(tmp_path):
    client = MagicMock(spec=MailCLI)
    client.sync_fetch.return_value = {'success': True, 'errors': [], 'summary': None}
    drain = MagicMock(return_value={'complete': True, 'errors': [], 'acked': 0, 'created': 0, 'backlog_hit': False})
    with patch('features.billing.sync_service.config_loader.load_config', return_value={}):
        sync_and_consume(client=client, todo_db_path=str(tmp_path / 'todo.db'), drain_fn=drain)
        assert client.sync_fetch.call_args.kwargs['provider'] == '163'
        sync_and_consume(client=client, account='explicit@example.invalid', todo_db_path=str(tmp_path / 'todo.db'), drain_fn=drain)
        assert client.sync_fetch.call_args.kwargs['account'] == 'explicit@example.invalid'


def tool_call(name, arguments, ident):
    return {'id': ident, 'name': name, 'arguments': json.dumps(arguments)}


@pytest.mark.parametrize('receipt', ['同步完成', '部分完成：其他邮箱超时'])
def test_interleaved_repeated_fetch_reuses_receipt_and_new_run_fetches_again(receipt):
    invoker = MagicMock()
    rounds = [
        _make_sync_result(tool_calls=[tool_call('billing_fetch', {}, 'one')]),
        _make_sync_result(tool_calls=[tool_call('billing_snapshot', {}, 'two')]),
        _make_sync_result(tool_calls=[tool_call('billing_fetch', {'months': 1, 'account': ''}, 'three')]),
        _make_sync_result(content='分析与邮件已完成'),
    ]
    invoker.invoke_sync.side_effect = rounds * 2
    engine = MagicMock()
    engine.is_read_only.return_value = False
    def reuse_key(name, raw):
        if name != 'billing_fetch':
            return None
        values = json.loads(raw)
        values.setdefault('months', 1)
        values.setdefault('account', '')
        return json.dumps(values, sort_keys=True)
    engine.get_request_reuse_key.side_effect = reuse_key
    engine.execute_with_context.side_effect = [receipt, '快照', receipt, '快照']
    runtime = AgentRuntime(invoker, engine, max_steps=5)
    tools = [{'type': 'function', 'function': {'name': n}} for n in ('billing_fetch', 'billing_snapshot')]
    for _ in range(2):
        events = list(runtime.run([{'role': 'user', 'content': '拉取分析帐单'}], tools, _make_ctx(), stream=False))
        results = [e for e in events if e.type == RuntimeEventType.TOOL_RESULT]
        assert results[-1].data['cached'] is True
        assert receipt in results[-1].data['output']
        assert any(e.type == RuntimeEventType.DONE for e in events)
    names = [c.args[1] for c in engine.execute_with_context.call_args_list]
    assert names == ['billing_fetch', 'billing_snapshot', 'billing_fetch', 'billing_snapshot']


def test_other_mutating_tools_are_not_cached():
    invoker = MagicMock()
    invoker.invoke_sync.side_effect = [
        _make_sync_result(tool_calls=[tool_call('todo_update', {'id': 'a'}, 'one')]),
        _make_sync_result(tool_calls=[tool_call('todo_update', {'id': 'a'}, 'two')]),
        _make_sync_result(content='完成'),
    ]
    engine = MagicMock()
    engine.is_read_only.return_value = False
    engine.execute_with_context.return_value = '已更新'
    list(AgentRuntime(invoker, engine, max_steps=4).run(
        [{'role': 'user', 'content': '修改待办'}], [], _make_ctx(), stream=False))
    assert engine.execute_with_context.call_count == 2


def test_reuse_is_explicit_metadata_and_uses_schema_defaults():
    @skill(name='fixture_reuse', description='fixture', reuse_within_run=True,
           params={'months': {'type': 'integer', 'default': 1}})
    def fixture_reuse(months=1):
        return 'fixture'

    engine = SkillEngine.__new__(SkillEngine)
    assert engine.get_request_reuse_key('fixture_reuse', '{}') == engine.get_request_reuse_key('fixture_reuse', '{"months":1}')
    assert engine.get_request_reuse_key('fixture_reuse', '{"months":2}') != engine.get_request_reuse_key('fixture_reuse', '{}')
    assert engine.get_request_reuse_key('fixture_reuse', '[]') is None
    assert engine.get_request_reuse_key('fixture_reuse', 'bad') is None
    assert engine.get_request_reuse_key('todo_update', '{}') is None
