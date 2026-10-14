from unittest.mock import Mock
import sqlite3
from pathlib import Path
from datetime import datetime
import pytest
from features.billing.repayment_todo import reconcile_repayment_todos,confirm_repayment
from features.todo.service import TodoService


def bill(**fields):
    r={'id':7,'bank_code':'CMB','account_name':'test@163.com','statement_month':'2026-10','created_at':'2026-10-01T00:00:00+00:00',
       'due_date':'2026-10-28','due_date_source':'extracted','is_paid':0,'total_due':'10','total_due_source':'extracted','balances':[{'currency':'CNY','total_due':'10'}],
       'purpose_availability':{'repayment_reminder':{'status':'available'}}}
    r.update(fields);return r


def client_for(*bills):
    client=Mock();client.run.return_value={'status':'ok','data':{'statements':list(bills)}};return client


def rows(db):
    if not Path(db).exists(): return []
    with sqlite3.connect(db) as c:
        c.row_factory=sqlite3.Row;return [dict(r) for r in c.execute("select * from todos where created_via != 'billing_notice'")]


def test_reminder_is_idempotent_and_only_explicit_payment_closes(tmp_path,monkeypatch):
    db=str(tmp_path/'todo.db');client=client_for(bill())
    assert reconcile_repayment_todos(client,db)['created']==1
    assert reconcile_repayment_todos(client,db)['created']==0
    row=rows(db)[0]
    assert row['kind']=='reminder' and row['recur_cron']=='12:00'
    assert row['due_at']=='2026-10-25T12:00:00+08:00'
    notices=[];monkeypatch.setattr('features.todo.service.send_im_alert',lambda *args,**kwargs:notices.append(args))
    service=TodoService(db)
    service.tick('2026-10-25T11:59:00+08:00');assert len(notices)==1; notices.clear()
    service.tick('2026-10-25T12:00:00+08:00');assert len(notices)==1
    service.tick('2026-10-25T12:01:00+08:00');assert len(notices)==1
    assert rows(db)[0]['status']=='pending'
    with pytest.raises(ValueError):confirm_repayment(7,client=client,db_path=db)
    client.mark_paid.assert_not_called()
    client.mark_paid.return_value={'updated':True,'statement_id':7}
    assert confirm_repayment(7,True,client,db)['todo_sync']=='done'
    service.tick('2026-10-26T12:00:00+08:00');assert len(notices)==1
    # Even a stale unpaid snapshot cannot resurrect the completed task.
    reconcile_repayment_todos(client,db)
    assert len(rows(db))==1 and rows(db)[0]['status']=='done'


@pytest.mark.parametrize('source',[None,'inferred','unknown'])
def test_unverified_date_never_schedules_a_reminder(tmp_path,source):
    db=str(tmp_path/'todo.db')
    stats=reconcile_repayment_todos(client_for(bill(due_date_source=source)),db)
    assert stats['verification']==1 and rows(db)[0]['due_at'] is None
    assert '核验' in rows(db)[0]['title']


def test_paid_sync_closes_manually_edited_task_without_overwriting_it(tmp_path):
    db=str(tmp_path/'todo.db');client=client_for(bill());reconcile_repayment_todos(client,db)
    tid=rows(db)[0]['id'];TodoService(db).update(tid,title='human title')
    client.run.return_value={'status':'ok','data':{'statements':[bill(is_paid=1)]}}
    assert reconcile_repayment_todos(client,db)['resolved']==1
    assert rows(db)[0]['title']=='human title' and rows(db)[0]['status']=='done'


def test_partial_metadata_still_reminds_to_verify_and_zero_is_not_paid(tmp_path):
    db=str(tmp_path/'todo.db')
    reconcile_repayment_todos(client_for(bill(purpose_availability={'repayment_reminder':{'status':'blocked'}})),db)
    assert rows(db)[0]['due_at'] and rows(db)[0]['status']=='pending'
    assert '核验' in rows(db)[0]['title']


def test_paid_before_creation_leaves_tombstone(tmp_path):
    db=str(tmp_path/'todo.db');reconcile_repayment_todos(client_for(bill(is_paid=1)),db)
    reconcile_repayment_todos(client_for(bill()),db)
    assert rows(db)==[]


def test_confirmed_payment_failure_is_reported_for_retry_and_does_not_fake_completion(tmp_path,monkeypatch):
    db=str(tmp_path/'todo.db');client=client_for(bill());reconcile_repayment_todos(client,db)
    client.mark_paid.return_value={'updated':True,'statement_id':7}
    monkeypatch.setattr(TodoService,'resolve_source',Mock(side_effect=OSError('disk unavailable')))
    result=confirm_repayment(7,True,client,db)
    assert result['updated'] is True and result['todo_sync']=='retry_required'
    assert rows(db)[0]['status']=='pending'


def test_enabled_sync_wires_repayment_adapter_without_second_mail_fetch(tmp_path,monkeypatch):
    from features.billing.sync_service import sync_and_consume
    monkeypatch.setattr('features.billing.sync_service.config_loader.load_config',lambda:{'billing':{'repayment_todo':{'enabled':True}}})
    client=client_for(bill())
    drain=lambda *args:{'complete':True,'acked':0,'created':0,'errors':[],'backlog_hit':False}
    result=sync_and_consume(client=client,todo_db_path=str(tmp_path/'todo.db'),run_fetch=False,drain_fn=drain)
    assert result['repayment_stats']['created']==1
    client.sync_fetch.assert_not_called()
    assert result['status']=='ok'


from tests.test_mail_quality_closeout import parser_repo

def test_real_parser_snapshot_proves_due_source_for_reminder(tmp_path,parser_repo):
    import json
    from integrations.mail.client import MailCLI
    root,sd,models=parser_repo
    db=str(tmp_path/'mail.db');sd.init_db(db)
    statement=models.StatementRecord(account_name='test@163.com',uid='repayment-real',bank_code='CMB',
        message_id='<repayment@example.invalid>',rule_id='CMB_TEMPLATE_V1',subject='账单',sender='test@example.invalid',email_date='2026-10-01',
        statement_month='2026-10',due_date='2026-10-28',total_due='10',minimum_due='1',
        raw_fields_json=json.dumps({'meta':{'due_date':{'value':'2026-10-28','source':'extracted'},'total_due':{'value':'10','source':'extracted'}},'quality':{'purpose_availability':{'repayment_reminder':{'status':'available','reasons':[]}}}}))
    sd.save_statement_with_transactions(db,statement,[])
    result=reconcile_repayment_todos(MailCLI(str(root),db_path=db),str(tmp_path/'todo.db'))
    assert result['created']==1 and result['verification']==0
    assert rows(str(tmp_path/'todo.db'))[0]['due_at'].endswith('+08:00')


def test_repeated_confirmation_repairs_todo_after_statement_already_paid(tmp_path):
    db=str(tmp_path/'todo.db');client=client_for(bill());reconcile_repayment_todos(client,db)
    client.mark_paid.return_value={'updated':False,'statement_id':7}
    client.run.return_value={'status':'ok','data':{'statements':[bill(is_paid=1)]}}
    result=confirm_repayment(7,True,client,db)
    assert result['already_paid'] is True and result['todo_sync']=='done'
    assert rows(db)[0]['status']=='done'


def test_initial_enrollment_skips_old_unpaid_backlog_but_resolves_old_paid(tmp_path):
    db=str(tmp_path/'todo.db')
    old=bill(id=8,statement_month='2026-09')
    result=reconcile_repayment_todos(client_for(old,bill()),db)
    assert result['created']==1 and rows(db)[0]['source_key']=='billing_repayment:7'


def test_machine_due_correction_and_human_edits_are_respected(tmp_path):
    db=str(tmp_path/'todo.db');client=client_for(bill());reconcile_repayment_todos(client,db)
    client.run.return_value={'status':'ok','data':{'statements':[bill(updated_at='2026-10-02T00:00:00+00:00',due_date='2026-10-29')]}}
    reconcile_repayment_todos(client,db);assert rows(db)[0]['due_at'].startswith('2026-10-26')
    tid=rows(db)[0]['id'];TodoService(db).update(tid,due_at='2026-10-30T09:00:00+08:00')
    client.run.return_value={'status':'ok','data':{'statements':[bill(updated_at='2026-10-03T00:00:00+00:00',due_date='2026-10-31')]}}
    reconcile_repayment_todos(client,db);assert rows(db)[0]['due_at'].startswith('2026-10-30')


def test_quality_notice_fires_once_and_relapse_can_notify_again(tmp_path,monkeypatch):
    from features.billing.quality_todo import consume_quality_events
    db=str(tmp_path/'todo.db')
    event={'event_id':'quality-1','case_key':'format-case','event_type':'quality_detected','revision':1,'cycle_id':1,
           'diagnostics':[{'code':'FORMAT_CHANGED','field':'structure'}],'evidence_refs':[]}
    client=Mock();client.quality_events.return_value=[event];client.quality_ack.return_value={'acked':True}
    assert consume_quality_events(client,db)['created']==1
    alerts=[];monkeypatch.setattr('features.todo.service.send_im_alert',lambda *args,**kwargs:alerts.append(args))
    service=TodoService(db);service.tick();service.tick();assert len(alerts)==1
    event.update(event_type='quality_resolved',revision=2)
    consume_quality_events(client,db)
    event.update(event_type='quality_detected',revision=3,cycle_id=2)
    consume_quality_events(client,db);service.tick();assert len(alerts)==2


def test_daily_noon_repeats_until_manual_close_without_marking_paid(tmp_path,monkeypatch):
    db=str(tmp_path/'todo.db');client=client_for(bill());reconcile_repayment_todos(client,db)
    service=TodoService(db);alerts=[]
    monkeypatch.setattr('features.todo.service.send_im_alert',lambda **kwargs:alerts.append(kwargs) or True)
    service.tick('2026-10-24T12:00:00+08:00') # notice only, before repayment window
    assert len(alerts)==1
    service.tick('2026-10-25T03:59:00+00:00');assert len(alerts)==1
    service.tick('2026-10-25T04:00:00+00:00');assert len(alerts)==2
    service.tick('2026-10-25T12:00:30+08:00');assert len(alerts)==2
    service.tick('2026-10-26T12:01:00+08:00');assert len(alerts)==3 # catch up if noon was missed
    service.tick('2026-10-27T12:00:00+08:00');assert len(alerts)==4
    service.tick('2026-10-29T12:00:00+08:00');assert len(alerts)==5 # after due date
    tid=rows(db)[0]['id'];service.repo.update_todo_status_atomic(tid,'done')
    reconcile_repayment_todos(client,db)
    service.tick('2026-10-30T12:00:00+08:00');assert len(alerts)==5
    client.mark_paid.assert_not_called()


@pytest.mark.parametrize('amount',['0','-1','NaN'])
def test_nonpositive_or_invalid_bill_never_creates_payment_notices(tmp_path,amount):
    db=str(tmp_path/'todo.db')
    stats=reconcile_repayment_todos(client_for(bill(total_due=amount)),db)
    assert stats['notices']==0 and rows(db)==[]


def test_failed_push_releases_claim_for_retry_without_duplicate_after_success(tmp_path,monkeypatch):
    db=str(tmp_path/'todo.db');reconcile_repayment_todos(client_for(bill()),db)
    service=TodoService(db)
    sender=Mock(side_effect=[False,True,True]);monkeypatch.setattr('features.todo.service.send_im_alert',sender)
    service.tick('2026-10-24T12:00:00+08:00');service.tick('2026-10-24T12:00:10+08:00');service.tick('2026-10-24T12:00:20+08:00')
    assert sender.call_count==2


def test_im_sender_uses_configured_api_credentials_without_logging_them(monkeypatch):
    from features.todo.service import send_im_alert
    from io import BytesIO
    monkeypatch.delenv('API_AUTH_TOKEN',raising=False)
    monkeypatch.delenv('LITE_AGENT_ALERT_URL',raising=False)
    monkeypatch.setattr('core.config_loader.load_config',lambda:{'channels':{'api':{'port':8899,'auth_token':'fixture-only'}}})
    requests=[]
    monkeypatch.setattr('features.todo.service.urllib.request.urlopen',lambda request,timeout:requests.append(request) or BytesIO(b'{"status":"delivered"}'))
    assert send_im_alert('test','test') is True
    assert requests[0].full_url=='http://127.0.0.1:8899/api/v1/alert'
    assert requests[0].get_header('Authorization')=='Bearer fixture-only'
