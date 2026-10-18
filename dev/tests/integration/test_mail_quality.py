"""Independent regressions for event ordering, human ownership and real CLI delivery."""
import importlib
import json
import os
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from lite_agent.integrations.mail.client import MailCLI, MailCLIError
from lite_agent.integrations.mail.quality_todo import consume_quality_events
from lite_agent.skills import ops_todo as todo


def read_todo(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM todos').fetchone()
        return dict(row) if row else None


def detect(db, revision, cycle=1, **kwargs):
    return todo.upsert_source_todo('mail_quality:k', f'revision {revision}',
        description=f'description {revision}', source_metadata={'revision': revision, 'cycle_id': cycle},
        db_path=str(db), **kwargs)


def test_older_detection_cannot_overwrite_or_reopen_newer_cycle(tmp_path):
    db = tmp_path / 'todo.db'
    detect(db,1)
    todo.resolve_source_todo('mail_quality:k',revision=2,cycle_id=1,db_path=str(db))
    detect(db,3,2)
    result=detect(db,1)
    assert result['status']=='protected'
    row=read_todo(db)
    assert (row['status'],row['title'])==('pending','revision 3')
    assert json.loads(row['source_metadata_json'])['revision']==3
    result=todo.resolve_source_todo('mail_quality:k',revision=2,cycle_id=1,db_path=str(db))
    assert not result['resolved']
    assert read_todo(db)['status']=='pending'


def test_resolution_before_detection_leaves_durable_tombstone(tmp_path):
    db=tmp_path/'todo.db'
    result=todo.resolve_source_todo('mail_quality:k',revision=2,cycle_id=1,db_path=str(db))
    assert not result['resolved']
    assert detect(db,1)['status']=='protected'
    assert read_todo(db) is None
    assert detect(db,3,2)['created']


@pytest.mark.parametrize('status', ['dropped','snoozed','shelved'])
def test_resolution_respects_manual_state(tmp_path,status):
    db=tmp_path/'todo.db'
    detect(db,1)
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE todos SET status=?',(status,))
    result=todo.resolve_source_todo('mail_quality:k',revision=2,cycle_id=1,db_path=str(db))
    assert not result['resolved']
    assert read_todo(db)['status']==status
    assert json.loads(read_todo(db)['source_metadata_json'])['revision']==2


def test_manual_title_edit_owns_task_and_machine_details_remain_available(tmp_path,monkeypatch):
    db=tmp_path/'todo.db'
    res=detect(db,1)
    monkeypatch.setattr(todo,'DB_PATH',str(db))
    monkeypatch.setattr(todo,'MD_PATH',str(tmp_path/'personal.md'))
    assert '已更新' in todo.todo_update(id=res['id'],title='human decision')
    detect(db,2)
    row=read_todo(db)
    assert (row['title'],row['description'])==('human decision','description 1')
    meta=json.loads(row['source_metadata_json'])
    assert meta['user_edited'] and meta['machine_description']=='description 2'
    todo.resolve_source_todo('mail_quality:k',revision=3,cycle_id=1,db_path=str(db))
    assert read_todo(db)['status']=='pending'


def test_same_source_concurrent_delivery_has_one_todo_and_latest_revision(tmp_path):
    db=tmp_path/'todo.db'
    todo._ensure_schema(str(db))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda rev: detect(db,rev),[7,2,3,9,8,1,9,6,4,5]))
    assert len({r['id'] for r in results})==1
    row=read_todo(db)
    assert row['title']=='revision 9'
    assert json.loads(row['source_metadata_json'])['revision']==9
    assert 'revision 9' in (tmp_path/'personal.md').read_text()
    assert not list(tmp_path.glob('*.tmp'))


def test_failed_markdown_replace_does_not_fail_committed_todo(tmp_path,monkeypatch):
    db=tmp_path/'todo.db'
    monkeypatch.setattr(todo.os,'replace',Mock(side_effect=OSError('view unavailable')))
    assert detect(db,1)['created']
    assert read_todo(db)['title']=='revision 1'
    assert not list(tmp_path.glob('*.tmp'))


def test_failed_todo_write_never_acknowledges_event(tmp_path,monkeypatch):
    client=Mock()
    client.quality_events.return_value=[{'event_id':'e','case_key':'k','event_type':'quality_detected',
        'revision':1,'cycle_id':1,'diagnostics':[]}]
    monkeypatch.setattr('lite_agent.integrations.mail.quality_todo.upsert_source_todo',Mock(side_effect=OSError('disk full')))
    stats=consume_quality_events(client,str(tmp_path/'todo.db'))
    assert stats['failed']==1 and stats['acked']==0
    client.quality_ack.assert_not_called()


@pytest.mark.parametrize('event_type,revision', [('new_unknown_type',1),('quality_detected',None),('quality_detected',True)])
def test_malformed_or_unknown_events_are_not_acknowledged(tmp_path,event_type,revision):
    client=Mock()
    client.quality_events.return_value=[{'event_id':'e','case_key':'k','event_type':event_type,
        'revision':revision,'cycle_id':1,'diagnostics':[]}]
    stats=consume_quality_events(client,str(tmp_path/'todo.db'))
    assert stats['failed']==1 and stats['acked']==0
    client.quality_ack.assert_not_called()


def test_fetch_failure_is_reported_as_failed(tmp_path):
    client=Mock()
    client.quality_events.side_effect=MailCLIError('failed transport')
    stats=consume_quality_events(client,str(tmp_path/'todo.db'))
    assert stats['failed']==1 and stats['errors']
    client.quality_ack.assert_not_called()


@pytest.fixture
def parser_repo(tmp_path, monkeypatch):
    assert os.environ.get("TESTING") == "1", "parser_repo fixture must run under testing isolation"
    import tempfile
    assert os.path.realpath(str(tmp_path)).startswith(os.path.realpath(tempfile.gettempdir())), "tmp_path must be in system temp"
    candidate = os.environ.get('MAIL_STATEMENT_PARSER_DIR') or '/Users/lilee/Projects/dev/mail-statement-parser'
    root = Path(candidate)
    if not (root / 'mail_client.py').exists():
        pytest.skip('Sibling mail parser checkout is needed for real CLI integration')
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("MAIL_STATEMENT_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MAIL_STATEMENT_DB_PATH", str(tmp_path / "statements.db"))
    monkeypatch.setenv("MAIL_STATEMENT_REPORT_DIR", str(tmp_path / "validation-reports"))
    monkeypatch.setenv("MAIL_STATEMENT_DOWNLOAD_DIR", str(tmp_path / "email-downloads"))
    return root, importlib.import_module('statement_db'), importlib.import_module('statement_models')


def test_real_cli_delivery_resolution_relapse_and_no_bill_starvation(tmp_path,parser_repo):
    root,sd,models=parser_repo
    db=str(tmp_path/'mail.db'); todos=str(tmp_path/'todos.db')
    sd.init_db(db)
    def event(kind='quality_detected',uid='u1',revision=1,cycle=1):
        return models.QualityEventRecord(case_key='k',event_type=kind,revision=revision,cycle_id=cycle,
            bank_code='CMB',account_name='mailbox',template_version='v1',
            diagnostics=[models.Diagnostic(code='CURRENCY_UNKNOWN',reason='missing original currency')],
            evidence_refs=[{'account':'mailbox','uid':uid,'message_id':f'<{uid}@bank>'}])
    for i in range(5):
        sd.upsert_quality_event(db,models.QualityEventRecord(case_key=f'bill-{i}',event_type='statement_recorded'))
    eid=sd.upsert_quality_event(db,event())
    client=MailCLI(str(root),db_path=db)
    stats=consume_quality_events(client,todos,limit=1)
    assert (stats['created'],stats['acked'],stats['failed'])==(1,1,0)
    assert len(sd.get_unacked_quality_events(db))==5 # bill events reserved for repayment adapter
    sd.upsert_quality_event(db,event())
    assert consume_quality_events(client,todos)['total']==0
    sd.upsert_quality_event(db,event('quality_resolved'))
    stats=consume_quality_events(client,todos)
    assert stats['resolved']==1 and stats['acked']==1
    assert read_todo(todos)['status']=='done'
    sd.upsert_quality_event(db,event(uid='u2'))
    stats=consume_quality_events(client,todos)
    assert stats['updated']==1 and stats['acked']==1
    row=read_todo(todos)
    assert row['status']=='pending'
    assert json.loads(row['source_metadata_json'])['cycle_id']==2
    with pytest.raises(MailCLIError):
        client.quality_ack(eid,row['id'],revision=1)
    assert len(sd.get_unacked_quality_events(db))==5


def test_real_cmb_case_is_repeat_safe_through_parser_cli_and_todo(tmp_path,parser_repo,monkeypatch):
    from email.message import EmailMessage
    root,sd,_=parser_repo
    parser=importlib.import_module('statement_parser')
    db=str(tmp_path/'mail.db'); todos=str(tmp_path/'todos.db')
    sd.init_db(db)
    monkeypatch.setattr(parser,'DB_PATH',db)
    monkeypatch.setattr(parser,'RULES_DIR',str(root/'rules'))
    monkeypatch.setattr(parser,'VALIDATION_REPORT_DIR',str(tmp_path/'reports'))
    monkeypatch.setenv('MAIL_STATEMENT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('MAIL_STATEMENT_DB_PATH', db)
    monkeypatch.setenv('MAIL_STATEMENT_DOWNLOAD_DIR', str(tmp_path / 'email-downloads'))
    monkeypatch.setenv('MAIL_STATEMENT_REPORT_DIR', str(tmp_path / 'reports'))
    msg=EmailMessage()
    msg['Subject']='招商银行信用卡电子账单'
    msg['From']='ccsvc@message.cmbchina.com'
    msg['Date']='Sat, 10 Oct 2026 10:00:00 +0800'
    msg['Message-ID']='<real-case@bank>'
    msg.set_content('2026/09/11 - 2026/10/10\n到期还款日：2026/10/28\n')
    msg.add_alternative('<p>2026/09/11 - 2026/10/10</p><p>到期还款日：2026/10/28</p>'
        '<table><tr><td>本期应还金额</td><td></td><td></td><td></td><td>¥100.00</td><td></td><td>¥10.00</td></tr>'
        '<tr><td>上期应还金额</td><td></td><td></td><td></td><td>¥0.00</td><td></td><td>¥0.00</td></tr></table>'
        '<table><tr><td>序号</td><td>交易日</td><td>记账日</td><td>交易描述</td><td>人民币金额</td><td>币种</td><td>地区</td><td>原币金额</td></tr>'
        '<tr><td>1</td><td>1001</td><td>1002</td><td>GOOGLE*CHATGPT</td><td>121.93</td><td>CNY</td><td>JP</td><td>2860.00</td></tr></table>',subtype='html')
    rules=parser.load_rule_files()
    result=parser.validate_and_save_email_message(msg,'uid-case',rules=rules,account_name='mailbox')
    assert result['status']=='saved'
    assert 'CURRENCY_UNKNOWN' in {d['code'] for d in result['diagnostics']}
    client=MailCLI(str(root),db_path=db)
    first=consume_quality_events(client,todos)
    assert first['created']>=1 and first['acked']==first['created'] and first['failed']==0
    parser.validate_and_save_email_message(msg,'uid-case',rules=rules,account_name='mailbox')
    assert consume_quality_events(client,todos)['total']==0
    with sqlite3.connect(db) as conn:
        row=conn.execute('SELECT amount,currency,original_amount,original_currency FROM statement_transactions').fetchone()
        assert row==('121.93','CNY','2860.00',None) # no guessed JPY and no fabricated FX rate


def test_schema_cache_tracks_replaced_default_database(tmp_path,monkeypatch):
    db=tmp_path/'default.db'
    monkeypatch.setattr(todo,'DB_PATH',str(db))
    todo._ensure_schema()
    # Replace with a different empty file at the same path, like restoring a database.
    replacement=tmp_path/'replacement.db'; replacement.touch()
    os.replace(replacement,db)
    todo._ensure_schema()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='todo_source_receipts'").fetchone()


@pytest.mark.parametrize('overrides', [
    {'schema_version':2}, {'command':'wrong_command'}, {'status':'partial'},
    {'data':[]}, {'errors':'invalid'}, {'errors':[None]},
])
def test_adapter_rejects_invalid_envelope(tmp_path,monkeypatch,overrides):
    from types import SimpleNamespace
    script=tmp_path/'mail_client.py';script.touch()
    payload={'schema_version':1,'command':'quality_events','status':'ok','data':{'events':[]},'errors':[]}
    payload.update(overrides)
    monkeypatch.setattr('lite_agent.integrations.mail.client.run_process',Mock(return_value=SimpleNamespace(
        stdout=json.dumps(payload),stderr='',returncode=0)))
    assert MailCLI(str(tmp_path)).run('quality_events')['status']=='error'


def test_real_cli_concurrent_consumers_share_one_todo(tmp_path,parser_repo):
    import subprocess
    import sys
    root,sd,models=parser_repo
    db=str(tmp_path/'mail.db');todos=str(tmp_path/'todos.db')
    sd.init_db(db)
    sd.upsert_quality_event(db,models.QualityEventRecord(case_key='concurrent-case',event_type='quality_detected',
        diagnostics=[models.Diagnostic(code='CURRENCY_UNKNOWN',reason='unknown')]))
    code='''import sys,json
from lite_agent.integrations.mail.client import MailCLI
from lite_agent.integrations.mail.quality_todo import consume_quality_events
print(json.dumps(consume_quality_events(MailCLI(sys.argv[1],db_path=sys.argv[2]),sys.argv[3])))'''
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'}
    processes=[subprocess.Popen([sys.executable,'-c',code,str(root),db,todos],cwd=Path(__file__).parents[3],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env) for _ in range(4)]
    results=[]
    for process in processes:
        stdout,stderr=process.communicate(timeout=30)
        assert process.returncode==0,stderr
        stats=json.loads(stdout.splitlines()[-1])
        assert not stats['failed'] and not stats['failed_acks'],json.dumps(stats,ensure_ascii=False)
        results.append(stats)
    assert sum(r['created'] for r in results)==1
    with sqlite3.connect(todos) as conn:
        assert conn.execute('SELECT COUNT(*) FROM todos').fetchone()[0]==1
    assert sd.get_unacked_quality_events(db)==[]
    assert 'CURRENCY_UNKNOWN' in (tmp_path/'personal.md').read_text()


def test_manual_start_claims_task_ownership(tmp_path,monkeypatch):
    db=tmp_path/'todo.db'
    res=detect(db,1)
    monkeypatch.setattr(todo,'DB_PATH',str(db))
    monkeypatch.setattr(todo,'MD_PATH',str(tmp_path/'personal.md'))
    todo.todo_start(id=res['id'])
    result=todo.resolve_source_todo('mail_quality:k',revision=2,cycle_id=1,db_path=str(db))
    assert not result['resolved']
    assert read_todo(db)['status']=='active'


def test_real_cli_racing_ack_preserves_new_event_and_retry_updates_existing_todo(tmp_path,parser_repo):
    root,sd,models=parser_repo
    db=str(tmp_path/'mail.db');todos=str(tmp_path/'todos.db')
    sd.init_db(db)
    def event(uid):
        return models.QualityEventRecord(case_key='ack-race',event_type='quality_detected',
            diagnostics=[models.Diagnostic(code='CURRENCY_UNKNOWN',reason='unknown')],
            evidence_refs=[{'uid':uid}])
    sd.upsert_quality_event(db,event('1'))
    client=MailCLI(str(root),db_path=db)
    real_ack=client.quality_ack
    def racing_ack(**kwargs):
        sd.upsert_quality_event(db,event('2'))
        return real_ack(**kwargs)
    client.quality_ack=racing_ack
    first=consume_quality_events(client,todos)
    assert first['created']==1 and first['failed_acks']==1 and first['acked']==0
    original_id=read_todo(todos)['id']
    assert sd.get_unacked_quality_events(db)[0]['revision']==2
    client.quality_ack=real_ack
    retry=consume_quality_events(client,todos)
    assert retry['updated']==1 and retry['acked']==1 and retry['failed_acks']==0
    assert read_todo(todos)['id']==original_id
    assert json.loads(read_todo(todos)['source_metadata_json'])['revision']==2
    assert sd.get_unacked_quality_events(db)==[]


@pytest.mark.parametrize('code,label', [('FORMAT_BASELINE_MISSING','模板基线待确认'),('FORMAT_CHANGED','账单模板变化'),('SOURCE_FIELD_MISSING','原文缺少关键字段'),('EXTRACTION_FAILED','关键字段提取失败')])
def test_template_and_missing_field_real_cli_todo_lifecycle(tmp_path,parser_repo,code,label):
    root,sd,models=parser_repo
    db=str(tmp_path/'mail.db'); todos=str(tmp_path/'todos.db')
    sd.init_db(db)
    record=models.QualityEventRecord(case_key='monitor:'+code,event_type='quality_detected',
        bank_code='CMB',account_name='mailbox',template_version='v1',
        diagnostics=[models.Diagnostic(code=code,field='structure' if code.startswith('FORMAT') else 'due_date')],
        evidence_refs=[{'account':'mailbox','uid':'monitor-1','message_id':'<monitor@bank>'}])
    client=MailCLI(str(root),db_path=db)
    sd.upsert_quality_event(db,record)
    assert consume_quality_events(client,todos)['created']==1
    assert label in read_todo(todos)['title']
    sd.upsert_quality_event(db,record)
    assert consume_quality_events(client,todos)['total']==0
    record.event_type='quality_resolved'
    sd.upsert_quality_event(db,record)
    assert consume_quality_events(client,todos)['resolved']==1
    assert read_todo(todos)['status']=='done'


def test_report_original_evidence_is_visible_without_fabricated_currency():
    from lite_agent.features.billing.snapshot import render_report,model_evidence
    snapshot={'transactions':[{'id':1483,'description':'GOOGLE*CHATGPT','amount':'125.55',
        'currency':'CNY','original_amount':'2860.00','original_currency_token':'¥',
        'original_currency_confirmed':False,'original_currency':None,'txn_location_code':'JP'}],
        'transaction_count':1,'unknown_currency_transactions':[]}
    report=render_report(snapshot)
    assert '2860.00' in report and '¥' in report and '未确认' in report
    assert '地点代码（非币种）' in report and 'JP' in report
    assert 'JPY' not in report
