"""Independent Stage 4 regressions for single execution, stores and truthful status."""
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from integrations.mail.client import MailCLI, MailCLIError
from integrations.mail.sync_service import sync_and_consume, drain_quality_events
from integrations.mail.sync_receipt import evaluate_sync_receipt
from core.billing_workflow import BillingWorkflow
from skills import ops_billing, ops_todo
from tests.test_mail_quality_closeout import parser_repo


@pytest.fixture(autouse=True)
def isolate_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(ops_todo, 'DB_PATH', str(tmp_path/'default-todos.db'))
    monkeypatch.setattr(ops_todo, 'MD_PATH', str(tmp_path/'personal.md'))
    monkeypatch.setattr('integrations.mail.sync_service.config_loader.load_config', lambda: {})


def receipt(account='box', **kwargs):
    return '--- JSON_SUMMARY_START ---\n'+json.dumps({'accounts':[{'account':account,'status':'ok',**kwargs}]})+'\n--- JSON_SUMMARY_END ---'


def response(stdout=None):
    return SimpleNamespace(returncode=0,stdout=stdout or receipt(),stderr='')


def test_runner_type_error_does_not_refetch_or_drop_database_env(tmp_path):
    runner=Mock(side_effect=TypeError('runner failed after starting child'))
    db=str(tmp_path/'explicit.db')
    result=MailCLI(str(tmp_path),db_path=db).sync_fetch(runner=runner)
    assert runner.call_count==1
    assert runner.call_args.kwargs['env']['MAIL_STATEMENT_DB_PATH']==db
    assert result['success'] is False and 'TypeError' in result['errors'][0]


def test_workflow_uses_own_default_database_not_unrelated_global_config(tmp_path,monkeypatch):
    monkeypatch.setattr('integrations.mail.sync_service.config_loader.load_config',lambda:{'billing':{'statement_db_path':'/unrelated.db'}})
    runner=Mock(return_value=response())
    with patch('integrations.mail.client.run_process',runner), patch.object(MailCLI,'quality_events',return_value=[]):
        _,errors=BillingWorkflow({},Mock()).fetch(str(tmp_path),1)
    assert errors==[]
    assert runner.call_args.kwargs['env']['MAIL_STATEMENT_DB_PATH']==str(tmp_path/'statements.db')


def test_skill_forwards_configured_store_paths_and_real_parser_counter(tmp_path,monkeypatch):
    config={'billing':{'statement_db_path':str(tmp_path/'mail.db'),'todo_db_path':str(tmp_path/'chosen-todo.db')}}
    monkeypatch.setattr(ops_billing,'_cfg',config)
    result={'summary':{'accounts':[{'account':'box','status':'ok','new_bills':7}]},'fetch_errors':[],
            'quality_stats':{'total':0,'errors':[]}}
    with patch('integrations.mail.sync_service.sync_and_consume',return_value=result) as sync:
        output=ops_billing.billing_fetch()
    assert sync.call_args.kwargs['todo_db_path']==config['billing']['todo_db_path']
    assert sync.call_args.kwargs['statement_db_path']==config['billing']['statement_db_path']
    assert '新增 7 封' in output


def test_default_todo_config_and_unexpected_fetch_failure_still_drains(tmp_path,monkeypatch):
    wanted=str(tmp_path/'chosen-todo.db')
    monkeypatch.setattr('integrations.mail.sync_service.config_loader.load_config',lambda:{'billing':{'todo_db_path':wanted}})
    client=Mock()
    client.sync_fetch.side_effect=RuntimeError('password: hidden-value')
    client.quality_events.return_value=[]
    with patch('integrations.mail.sync_service.consume_quality_events',return_value={'total':0}) as consume:
        result=sync_and_consume(client=client)
    assert client.sync_fetch.call_count==1
    assert consume.call_args.kwargs['db_path']==wanted
    assert result['status']=='failed' and result['quality_success']
    assert 'hidden-value' not in json.dumps(result)


def test_failed_quality_read_never_reports_empty_queue(tmp_path):
    client=Mock()
    client.quality_events.side_effect=MailCLIError('token=hidden-value')
    client.sync_fetch.return_value={'success':True,'summary':{'accounts':[{'status':'ok'}]},'errors':[]}
    result=sync_and_consume(client=client)
    assert result['status']=='partial'
    assert not result['quality_success']
    assert result['quality_stats']['stop_reason']=='error'
    assert result['quality_stats']['backlog'] is None
    assert 'hidden-value' not in json.dumps(result)
    with patch('integrations.mail.sync_service.sync_and_consume',return_value=result):
        output=ops_billing.billing_fetch()
    assert '无法判断' in output and '无待处理解析异常' not in output


def test_backlog_probe_failure_is_not_complete(tmp_path):
    client=Mock()
    client.quality_events.side_effect=MailCLIError('probe failed')
    with patch('integrations.mail.sync_service.consume_quality_events',return_value={'total':1,'acked':1}):
        stats=drain_quality_events(client,event_limit=1,max_batches=1)
    assert stats['failed']==1 and not stats['complete']
    assert stats['backlog'] is None and stats['stop_reason']=='probe_failed'
    assert stats['errors']


def test_short_batch_does_not_hide_a_newly_arrived_event(tmp_path):
    client=Mock();client.quality_events.return_value=[{'event_id':'new-arrival'}]
    with patch('integrations.mail.sync_service.consume_quality_events',return_value={'total':1,'acked':1}):
        stats=drain_quality_events(client,event_limit=100)
    assert stats['batches']==1 and not stats['complete']
    assert stats['backlog'] and not stats['backlog_hit']


def test_no_progress_is_incomplete_even_without_write_failure():
    client=Mock()
    with patch('integrations.mail.sync_service.consume_quality_events',return_value={'total':1,'skipped':1}):
        stats=drain_quality_events(client)
    assert stats['stop_reason']=='no_progress' and not stats['complete']
    assert stats['errors'] and stats['backlog']


@pytest.mark.parametrize('kwargs',[{'event_limit':0},{'max_batches':0},{'months':False},{'max_batches':'3'}])
def test_invalid_budgets_fail_before_fetch(kwargs):
    client=Mock()
    with pytest.raises(ValueError):
        sync_and_consume(client=client,**kwargs)
    client.sync_fetch.assert_not_called()


def test_caller_cannot_exceed_cost_ceiling():
    client=Mock();client.quality_events.return_value=[{'event_id':'remaining'}]
    with patch('integrations.mail.sync_service.consume_quality_events',return_value={'total':100,'acked':100}) as consume:
        stats=drain_quality_events(client,event_limit=10000,max_batches=10000)
    assert consume.call_count==3 and stats['total']==300
    assert all(call.kwargs['limit']==100 for call in consume.call_args_list)
    assert stats['backlog_hit']


@pytest.mark.parametrize('suffix',['--- JSON_SUMMARY_START ---\n{truncated',
                                  '--- JSON_SUMMARY_START ---\n{bad}\n--- JSON_SUMMARY_END ---'])
def test_last_broken_receipt_cannot_reuse_prior_success(suffix):
    result=evaluate_sync_receipt(0,receipt()+'\n'+suffix)
    assert result['summary'] is None and not result['success']
    assert '损坏' in result['errors'][0]


@pytest.mark.parametrize('count',['0',-1,False,None])
def test_malformed_counters_cannot_produce_success(count):
    result=evaluate_sync_receipt(0,receipt(errors=count))
    assert not result['success'] and result['errors']


def test_masking_preserves_json_shape_and_masks_public_summary():
    result=evaluate_sync_receipt(0,receipt(password='secret-value',error='password: "secret-value"'))
    assert not result['success']
    assert result['summary']['accounts'][0]['password']=='***'
    assert json.loads(json.dumps(result['summary']))['accounts'][0]['status']=='ok'


def test_cron_calls_same_billing_fetch_once_and_registers_no_extra_fetch_job(monkeypatch):
    fetch=Mock(return_value='completed')
    monkeypatch.setattr(ops_billing,'billing_fetch',fetch)
    before=len(ops_billing._mgr.jobs)
    assert ops_billing._billing_fetch_cron() is None
    fetch.assert_called_once_with(months=1)
    assert len(ops_billing._mgr.jobs)==before


def test_injected_client_cannot_ack_a_different_explicit_database(tmp_path):
    client=MailCLI(str(tmp_path),db_path=str(tmp_path/'wrong.db'))
    with pytest.raises(ValueError,match='database'):
        sync_and_consume(client=client,statement_db_path=str(tmp_path/'desired.db'))


def quality_event(key):
    return {'event_id':key,'case_key':key,'event_type':'quality_detected',
            'revision':1,'cycle_id':1,'diagnostics':[]}


def test_first_write_failure_stops_inside_batch(tmp_path):
    client=Mock();client.quality_events.return_value=[quality_event('one'),quality_event('two')]
    with patch('integrations.mail.quality_todo.upsert_source_todo',side_effect=OSError('write failed')) as write:
        stats=drain_quality_events(client,str(tmp_path/'todos.db'))
    assert write.call_count==1 and stats['failed']==1
    client.quality_ack.assert_not_called()
    assert not stats['complete']


def test_first_ack_rejection_stops_inside_batch(tmp_path):
    client=Mock();client.quality_events.return_value=[quality_event('one'),quality_event('two')]
    client.quality_ack.return_value={'acked':False,'reason':'revision conflict'}
    with patch('integrations.mail.quality_todo.upsert_source_todo',return_value={'id':'todo','status':'created'}) as write:
        stats=drain_quality_events(client,str(tmp_path/'todos.db'))
    assert write.call_count==1 and client.quality_ack.call_count==1
    assert stats['failed_acks']==1 and not stats['complete']


def test_budget_expiry_after_todo_write_keeps_event_unacked(tmp_path):
    client=Mock();client.quality_events.return_value=[quality_event('one'),quality_event('two')]
    with patch('integrations.mail.quality_todo.time.monotonic',side_effect=[0,0,0,31]), patch(
            'integrations.mail.quality_todo.upsert_source_todo',return_value={'id':'todo','status':'created'}) as write:
        stats=drain_quality_events(client,str(tmp_path/'todos.db'))
    assert write.call_count==1 and stats['failed_acks']==1
    client.quality_ack.assert_not_called()
    assert '预算耗尽' in stats['errors'][0]
    assert not stats['complete']


def test_escaped_secret_and_truncated_receipt_never_leak():
    for output in [receipt(password='secret-value',error='password: "secret-value"'),
                   '--- JSON_SUMMARY_START ---\n{"password":"secret-value"']:
        result=evaluate_sync_receipt(0,output)
        assert 'secret-value' not in json.dumps(result)


def test_fetch_writes_event_to_same_store_consumed_by_real_cli(tmp_path,parser_repo):
    root,sd,models=parser_repo
    wanted=str(tmp_path/'mail.db');todos=str(tmp_path/'todos.db')
    calls=[]
    def fetch_runner(*args,**kwargs):
        calls.append(kwargs)
        database=kwargs['env']['MAIL_STATEMENT_DB_PATH']
        assert database==wanted
        sd.init_db(database)
        sd.upsert_quality_event(database,models.QualityEventRecord(
            case_key='fetch-produced',event_type='quality_detected',revision=1,cycle_id=1,
            bank_code='CMB',account_name='test-box',template_version='v1',
            diagnostics=[models.Diagnostic(code='CURRENCY_UNKNOWN',reason='missing currency')],
            evidence_refs=[{'account':'test-box','uid':'uid1','message_id':'<1@bank>'}]))
        return response()
    result=sync_and_consume(script_dir=str(root),statement_db_path=wanted,todo_db_path=todos,runner=fetch_runner)
    assert len(calls)==1 and result['status']=='ok'
    assert result['quality_stats']['created']==result['quality_stats']['acked']==1
    with sqlite3.connect(todos) as conn:
        assert conn.execute("SELECT count(*) FROM todos WHERE source_key='mail_quality:fetch-produced'").fetchone()[0]==1
    assert MailCLI(str(root),db_path=wanted).quality_events()==[]
