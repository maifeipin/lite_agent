from datetime import date
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.billing_snapshot import build_snapshot, decimal_amount, render_report
from core.billing_workflow import BillingWorkflow, _parse_json_summary
from core.execution_budget import ExecutionBudget, BudgetAdmissionError
from core.llm_gateway import LLMGateway
from core.output_delivery import prepare_channel_output
from core.task_result import TaskResult
from core.loop_detector import LoopDetector
from core.worker_agent import WorkerAgent, WorkerOutcome
from core.subtask_dag import Subtask
from core.task_orchestrator import TaskOrchestrator
from core.model_policy import ExecutionPolicy
import threading
import pytest


@pytest.fixture
def billing_db(tmp_path):
    path = tmp_path / 'statements.db'
    c = sqlite3.connect(path)
    c.executescript('''
      CREATE TABLE statements(id INTEGER PRIMARY KEY, account_name TEXT, uid TEXT,
        bank_code TEXT, statement_month TEXT, due_date TEXT, total_due TEXT,
        minimum_due TEXT, is_paid INTEGER, created_at TEXT);
      CREATE TABLE statement_transactions(id INTEGER PRIMARY KEY, account_name TEXT, uid TEXT,
        bank_code TEXT, txn_date TEXT, description TEXT, amount TEXT, currency TEXT,
        direction TEXT, original_amount TEXT);
      INSERT INTO statements VALUES (1,'a','1','CMB','2026-10','2026-10-08','10.10','1',0,'2026-10-01');
      INSERT INTO statements VALUES (2,'b','1','HX','2026-09','2026-10-20','','',1,'2026-09-01');
      INSERT INTO statement_transactions VALUES (1,'a','1','CMB','2026-10-01','shop|a','0.10','CNY','debit','');
      INSERT INTO statement_transactions VALUES (2,'a','1','CMB','2026-10-02','shop2','0.20','CNY','debit','');
      INSERT INTO statement_transactions VALUES (3,'b','1','HX','2026-09-02','foreign','2.00','USD','debit','');
    ''')
    c.execute('ALTER TABLE statement_transactions ADD COLUMN currency_source TEXT')
    c.execute("UPDATE statement_transactions SET currency_source='extracted'")
    c.commit()
    c.close()
    return path


def test_snapshot_preserves_currency_precision_identity_and_freshness(billing_db):
    snapshot = build_snapshot(billing_db, today=date(2026, 10, 10), detail_limit=1)
    assert snapshot['transaction_count'] == 3
    assert snapshot['transaction_totals'] == [
        {'currency': 'CNY', 'direction': 'debit', 'amount': '0.30', 'count': 2},
        {'currency': 'USD', 'direction': 'debit', 'amount': '2.00', 'count': 1}]
    assert any('最新账期为 2026-09' in s for s in snapshot['anomalies'])
    assert any('需核实实际还款状态' in s for s in snapshot['anomalies'])
    assert any('应还金额缺失' in s for s in snapshot['anomalies'])
    report = render_report(snapshot)
    assert 'shop\\|a' in report and 'foreign' in report
    assert decimal_amount('unknown') is None
    assert decimal_amount('CNY 10') is None


def test_missing_database_is_not_created(tmp_path):
    missing = tmp_path / 'missing.db'
    with pytest.raises((sqlite3.OperationalError, RuntimeError)):
        build_snapshot(missing)
    assert not missing.exists()


def test_new_schema_anchors_are_authoritative_and_failed_statements_excluded(billing_db):
    with sqlite3.connect(billing_db) as c:
        c.execute("ALTER TABLE statements ADD COLUMN parse_status TEXT DEFAULT 'ok'")
        c.execute('ALTER TABLE statement_transactions ADD COLUMN statement_id INTEGER')
        c.execute('UPDATE statement_transactions SET statement_id=CASE WHEN id<3 THEN 1 ELSE 2 END')
        c.execute("UPDATE statements SET uid='new-uid' WHERE id=1")
        c.execute("UPDATE statements SET parse_status='failed',statement_month='2026-11' WHERE id=2")
        c.execute("INSERT INTO statement_transactions VALUES (4,'a','new-uid','CMB','2026-10-03','unanchored','99','CNY','debit','','extracted',NULL)")
    snapshot = build_snapshot(billing_db, today=date(2026, 10, 11))
    assert snapshot['database_statement_count'] == 2
    assert snapshot['failed_statement_count'] == 1
    assert [r['id'] for r in snapshot['statements']] == [1]
    assert [r['id'] for r in snapshot['transactions']] == [1, 2]
    assert snapshot['transaction_totals'][0]['amount'] == '0.30'
    assert any('解析失败' in s for s in snapshot['anomalies'])
    assert all(r['bank'] != 'HX' for r in snapshot['latest_months'])


@pytest.mark.parametrize('payload', ['[]', 'null', '{}', '{"accounts":[]}',
                                   '{"accounts":["bad"]}', '{"accounts":[{}]}', '{bad'])
def test_invalid_receipts_cannot_claim_success(payload):
    output = '--- JSON_SUMMARY_START ---\n' + payload + '\n--- JSON_SUMMARY_END ---'
    assert _parse_json_summary(output) is None
    workflow = BillingWorkflow({}, MagicMock())
    with patch('integrations.mail.client.run_process', return_value=SimpleNamespace(
            stdout=output, stderr='', returncode=0)):
        _, errors = workflow.fetch('/parser', 1)
    assert any('无法确认' in e for e in errors)


def test_fetch_uses_single_sync_and_surfaces_each_account_failure():
    receipt = {'accounts': [{'account': 'a', 'status': 'partial', 'errors': 1},
                            {'account': 'b', 'status': 'timeout', 'error': 'scan timed out'},
                            {'account': 'c', 'status': 'ok', 'parse_failed': 1}]}
    raw = '--- JSON_SUMMARY_START ---\n' + json.dumps(receipt) + '\n--- JSON_SUMMARY_END ---'
    with patch('integrations.mail.client.run_process', return_value=SimpleNamespace(
            stdout=raw, stderr='', returncode=0)) as run:
        _, errors = BillingWorkflow({}, MagicMock()).fetch('/dummy_parser', 3)
    assert run.call_count == 1
    assert run.call_args.args[0][-2:] == ['sync3m', '3']
    assert any('处理异常 1' in e for e in errors)
    assert any('timeout' in e for e in errors)
    assert any('解析失败 1' in e for e in errors)


@pytest.mark.parametrize('policy,allowed,expected', [
    (None, ['flash', 'glm'], 'flash'),
    (ExecutionPolicy.user_locked('glm'), ['flash', 'glm'], 'glm'),
    (None, ['glm'], 'glm'),
])
def test_billing_model_preference_preserves_user_lock_and_route_permissions(
        billing_db, tmp_path, policy, allowed, expected):
    config = {'llm': {'default': 'glm', 'models': {'flash': {}, 'glm': {}}},
        'billing': {'statement_db_path': str(billing_db), 'script_dir': str(tmp_path)},
        'task_routing': {'route_rules': [{'type': 'data_analysis', 'model': 'glm',
                                        'allowed_models': allowed}],
                         'billing_workflow': {'analysis_model': 'flash'}},
        'output_delivery': {'long_output': {'full_delivery': 'email'},
                            'sqlite': {'path': str(tmp_path / 'archive.db')}}}
    router = MagicMock()
    router.get_call_profile.return_value = {'max_tokens': 2048, 'timeout': 60, 'invoke_kwargs': {}}
    router.get_invoker.return_value.invoke_sync.return_value = {
        'content': '分析完成', 'usage_total': 500, 'finish_reason': 'stop'}
    workflow = BillingWorkflow(config, router)
    with patch.object(workflow, 'fetch', return_value=('', [])), \
         patch('core.output_delivery._send_email', return_value='qq'):
        result = workflow.run('拉取最新账单分析', 'model-policy-test', policy=policy)
    assert result.status == 'succeeded'
    assert router.get_invoker.call_args.args[0] == expected


def test_task_failure_survives_summary_and_email_is_only_a_receipt():
    result = TaskResult('partial', 'x' * 2000, failed_nodes=['分析'], skipped_nodes=['总结发送'])
    config = {'output_delivery': {'channel_limits': {'wechat': 160}}}
    with patch('core.output_delivery._send_email', return_value='qq'):
        text = prepare_channel_output(result.content, 'wechat', config,
            overrides={'full_delivery': 'email'}, task_result=result,
            summarize=lambda *_: '一切正常')
    assert '任务部分完成' in text and '分析' in text and '总结发送' in text
    assert '执行报告已提交' in text
    assert len(text) <= 160


def test_submitted_billing_report_is_not_sent_twice():
    result = TaskResult('succeeded', 'x' * 3000,
        delivery={'purpose': 'billing_report', 'status': 'submitted', 'destination': 'email', 'location': 'qq'})
    with patch('core.output_delivery._send_email') as send:
        text = prepare_channel_output(result.content, 'wechat', {}, task_result=result)
    send.assert_not_called()
    assert '账单报告已提交' in text


def test_alternating_cycle_is_detected():
    detector = LoopDetector()
    assert [detector.check(name, '{}') for name in ['a','b','a','b','a','b']] == [False] * 5 + [True]


def test_failed_worker_keeps_evidence():
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.direct_tool_execution = False
    orch._make_worker = MagicMock()
    orch._make_worker.return_value.run.return_value = WorkerOutcome(
        'partial analysis', [{'name': 'billing_report', 'result': 'real data'}],
        status='failed', terminal_reason='token_budget')
    orch._log_and_persist = MagicMock()
    result = {}
    orch._run_single_subtask(Subtask('a','analysis'), {}, result, threading.Lock())
    assert result['a']['status'] == 'failed'
    assert result['a']['tool_results'][0]['result'] == 'real data'
    assert result['a']['result'] == 'partial analysis'


def test_worker_upstream_evidence_is_not_in_system_prompt():
    engine = MagicMock()
    engine.list_skills_filtered.return_value = ''
    invoker = MagicMock()
    invoker.invoke_sync.return_value = {'content': 'done', 'tool_calls': [], 'usage_total': 1}
    worker = WorkerAgent('w', None, 'test', {}, engine, invoker=invoker)
    worker.run(Subtask('s','task',prompt='goal'), {'dep': {'result': 'upstream-marker' + 'x' * 50000}})
    messages = invoker.invoke_sync.call_args.kwargs['messages']
    assert 'upstream-marker' not in messages[0]['content']
    assert any('upstream-marker' in m.get('content','') for m in messages[1:])
    assert sum(len(m.get('content','')) for m in messages) < 12000


def test_budgeted_gateway_charges_unknown_usage_and_prevents_followup():
    budget = ExecutionBudget(2, 1000)
    invoker = MagicMock()
    invoker.invoke_sync.return_value = {'content': 'ok', 'usage_total': 0}
    gateway = LLMGateway(budget=budget)
    result = gateway.invoke_sync([{'role':'user','content':'hi'}], invoker=invoker, max_tokens=800)
    assert result['usage_estimated']
    assert budget.snapshot().used_tokens > 800
    with pytest.raises(BudgetAdmissionError):
        gateway.invoke_sync([{'role':'user','content':'hi'}], invoker=invoker, max_tokens=800)
    assert invoker.invoke_sync.call_count == 1


def test_case_uses_single_model_call_and_delivers_all_details(billing_db, tmp_path):
    config = {'llm': {'default':'flash','models': {'flash': {}}},
        'billing': {'script_dir':str(tmp_path), 'statement_db_path':str(billing_db)},
        'task_routing': {'route_rules':[{'type':'data_analysis','model':'flash'}],
                         'billing_workflow': {'enabled':True,'detail_limit':1}},
        'output_delivery': {'long_output': {'full_delivery':'email'},
                            'sqlite': {'path':str(tmp_path / 'archive.db')}}}
    router = MagicMock()
    router.get_call_profile.return_value = {'max_tokens':2048,'timeout':60,'invoke_kwargs':{}}
    invoker = MagicMock()
    invoker.invoke_sync.return_value = {'content':'按币种分析，未还款标记需核实。', 'usage_total':1234,
                                     'prompt_tokens':1000,'completion_tokens':234,'finish_reason':'stop'}
    router.get_invoker.return_value = invoker
    workflow = BillingWorkflow(config, router)
    assert workflow.matches('拉取最新账单，分析总结到邮箱')
    assert not workflow.matches('删除账单')
    assert not workflow.matches('拉取最新账单分析并发送到 x@example.com')
    with patch.object(workflow, 'fetch', return_value=('fetch done', [])), \
         patch('core.output_delivery._send_email', return_value='qq') as send:
        result = workflow.run('拉取最新账单，分析总结到邮箱', 'test')
    assert result.status == 'succeeded'
    assert result.usage['calls'] == 1 and result.usage['total_tokens'] == 1234
    assert invoker.invoke_sync.call_count == 1
    prompt = invoker.invoke_sync.call_args.kwargs['messages'][-1]['content']
    assert json.loads(prompt)['details_truncated'] is True
    assert 'shop2' in send.call_args.args[0] and 'foreign' in send.call_args.args[0]
    assert result.delivery['status'] == 'submitted'


def test_parallel_reservations_cannot_claim_same_allowance():
    budget = ExecutionBudget(5, 1000)
    barrier = threading.Barrier(2)
    winners = []
    def attempt():
        barrier.wait()
        try:
            winners.append(budget.reserve('worker', 700))
        except BudgetAdmissionError:
            pass
    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(winners) == 1
    assert budget.snapshot().remaining_tokens == 300
    budget.settle(winners[0], 200)
    assert budget.snapshot().remaining_tokens == 800


def test_sequential_workers_leave_allowance_for_following_nodes():
    budget = ExecutionBudget(30, 200000)
    budget.consume('planner', steps=1, tokens=5892)
    steps, tokens = budget.worker_share(1, pending_workers=3, reserve_tokens=8000)
    assert steps == 9 and tokens == 62036
    budget.consume('worker', steps=3, tokens=10000)
    assert budget.worker_share(1, pending_workers=2, reserve_tokens=8000)[1] == 88054


def test_explicit_budget_limits_reasoning_model_output():
    from core.model_invoker import OpenAIInvoker
    invoker = OpenAIInvoker(MagicMock(), 'reasoner-v2')
    kwargs = invoker._build_call_kwargs([{'role':'user','content':'hi'}], [], max_tokens=256)
    assert kwargs['max_tokens'] == 256


def test_low_yield_is_counted_per_round_not_number_of_tools():
    from core.adaptive_budget import AdaptiveBudgetPolicy, AdaptiveStepController, BudgetAction
    controller = AdaptiveStepController(20, AdaptiveBudgetPolicy(enabled=True, low_yield_streak=3))
    for step in (1,2):
        for n in range(20):
            controller.record_tool_result(step=step, tool_name='web_search',
                arguments=json.dumps({'q':str(n)}), ok=True, output='无结果')
    assert controller.before_step(3).action == BudgetAction.EXECUTE
    for n in range(20):
        controller.record_tool_result(step=3, tool_name='web_search',
            arguments=json.dumps({'q':str(n)}), ok=True, output='无结果')
    assert controller.before_step(4).action == BudgetAction.REPLAN
    for step in (4,5):
        for n in range(20):
            controller.record_tool_result(step=step, tool_name='web_search', arguments='{}', ok=True, output='无结果')
    assert controller.before_step(6).action == BudgetAction.FINALIZE


def test_structured_report_does_not_pay_for_second_summary():
    result = TaskResult('partial','x' * 3000, failed_nodes=['分析'],
        delivery={'purpose':'billing_report','status':'submitted','destination':'email','location':'qq'})
    summarize = MagicMock(return_value='fake success')
    output = prepare_channel_output(result.content, 'wechat', {}, task_result=result, summarize=summarize)
    summarize.assert_not_called()
    assert '任务部分完成' in output


def test_deepseek_thinking_switch_is_translated_without_affecting_other_vendors():
    from core.model_invoker import OpenAIInvoker
    ds = OpenAIInvoker(MagicMock(), 'deepseek-v4-flash', provider='deepseek')
    request = ds._build_call_kwargs([], [], extra_body={'enable_thinking':False}, max_tokens=2048)
    assert request['extra_body'] == {'thinking':{'type':'disabled'}}
    assert request['max_tokens'] == 2048
    generic = OpenAIInvoker(MagicMock(), 'qwen')
    assert generic._build_call_kwargs([], [], extra_body={'enable_thinking':False})['extra_body'] == {'enable_thinking':False}
