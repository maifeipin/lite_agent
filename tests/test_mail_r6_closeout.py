import os
import importlib
import json
import sqlite3
from datetime import date, datetime, timezone
from email.message import EmailMessage

import pytest

from features.billing.snapshot import build_snapshot, model_evidence, render_report
from tests.test_mail_quality_closeout import parser_repo


@pytest.fixture
def mail_store(tmp_path, parser_repo, monkeypatch):
    assert os.environ.get("TESTING") == "1", "mail_store must run under testing isolation"
    assert "temp" in str(tmp_path).lower() or "tmp" in str(tmp_path).lower() or "var" in str(tmp_path).lower(), "mail_store must bind to temporary path"
    root, db_module, models = parser_repo
    parser = importlib.import_module('statement_parser')
    readmodel = importlib.import_module('billing_readmodel')
    db = str(tmp_path / 'statements.db')
    db_module.init_db(db)
    monkeypatch.setattr(parser, 'DB_PATH', db)
    monkeypatch.setattr(parser, 'RULES_DIR', str(root / 'rules'))
    monkeypatch.setattr(parser, 'VALIDATION_REPORT_DIR', str(tmp_path / 'reports'))
    monkeypatch.setenv('MAIL_STATEMENT_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('MAIL_STATEMENT_DB_PATH', db)
    monkeypatch.setenv('MAIL_STATEMENT_DOWNLOAD_DIR', str(tmp_path / 'email-downloads'))
    monkeypatch.setenv('MAIL_STATEMENT_REPORT_DIR', str(tmp_path / 'reports'))
    return db, db_module, models, parser, readmodel


def stored_statement(models, uid='sample', total='221.93', raw_fields=None):
    return models.StatementRecord(uid=uid, message_id=f'<{uid}@sample.invalid>', bank_code='CMB',
        rule_id='CMB_TEMPLATE_V1', subject='招商银行信用卡电子账单',
        sender='ccsvc@message.cmbchina.com', email_date='Sat, 10 Oct 2026 10:00:00 +0800',
        account_name='test-account', statement_month='2026-10', statement_date='2026-10-10',
        due_date='2026-10-28', total_due=total, minimum_due='10.00',
        raw_fields_json=json.dumps(raw_fields or {}))


def test_unverified_currency_does_not_enter_confirmed_currency_totals_in_either_entry(mail_store):
    db, sd, models, _, rm = mail_store
    statement = stored_statement(models)
    transactions = [models.StatementTransactionRecord(uid=statement.uid, bank_code='CMB',
        account_name='test-account', txn_date='2026-10-01', description='MERCHANT', amount=amount,
        currency='CNY', currency_source=source, direction='debit')
        for amount, source in [('121.93', None), ('33.00', 'legacy_assumed'), ('10.00', 'rule_declared')]]
    sd.save_statement_with_transactions(db, statement, transactions)
    modern = rm.snapshot(db, today=datetime(2026,10,11,tzinfo=timezone.utc))
    legacy = build_snapshot(db, today=date(2026,10,11))
    for snapshot in (modern,legacy):
        confirmed = [r for r in snapshot['transaction_totals'] if r['currency']=='CNY']
        assert len(confirmed)==1 and confirmed[0]['amount']=='10.00'
        assert snapshot.get('unknown_currency_transactions')
    evidence=model_evidence(legacy)
    assert evidence.get('unknown_currency_transactions')
    # User report must retain evidence rather than silently dropping the uncertain records.
    report=render_report(legacy)
    assert '121.93' in report and '33.00' in report


def message_with_table(headers, rows, total='221.93', extra=''):
    message=EmailMessage()
    message['Subject']='招商银行信用卡电子账单'
    message['From']='ccsvc@message.cmbchina.com'
    message['Date']='Sat, 10 Oct 2026 10:00:00 +0800'
    message['Message-ID']='<cmb-r6-case@sample.invalid>'
    message.set_content('2026/09/11 - 2026/10/10\n到期还款日：2026/10/28\n')
    table='<table><tr>'+''.join(f'<td>{cell}</td>' for cell in headers)+'</tr>'
    for row in rows:
        table+='<tr>'+''.join(f'<td>{cell}</td>' for cell in row)+'</tr>'
    table+='</table>'
    html=('<p>2026/09/11 - 2026/10/10</p><p>到期还款日：2026/10/28</p>'
          f'<table><tr><td>本期应还金额</td><td></td><td></td><td></td><td>¥{total}</td><td></td><td>¥10.00</td></tr>'
          '<tr><td>上期应还金额</td><td></td><td></td><td></td><td>¥0.00</td><td></td><td>¥0.00</td></tr></table>'+table+extra)
    message.add_alternative(html,subtype='html')
    return message


HEADER=['序号','交易日','记账日','交易描述','人民币金额','币种','地区','原币金额','原币币种']
ROWS=[['1','1001','1002','FOREIGN SERVICE','100.00','CNY','US','20.00','USD'],
      ['2','1001','1002','GOOGLE*CHATGPT','121.93','CNY','JP','2860.00','']]


def parse_case(mail_store, headers=HEADER, rows=ROWS, **kwargs):
    db, _, _, parser, _ = mail_store
    result=parser.validate_and_save_email_message(message_with_table(headers,rows,**kwargs),
        'stable-uid',rules=parser.load_rule_files(),account_name='test-account')
    assert result['status']=='saved'
    with sqlite3.connect(db) as conn:
        conn.row_factory=sqlite3.Row
        transactions=[dict(row) for row in conn.execute('SELECT * FROM statement_transactions ORDER BY id')]
    return result,transactions


def test_known_currency_on_one_row_does_not_hide_unknown_on_another(mail_store):
    result,transactions=parse_case(mail_store)
    assert len(transactions)==2
    assert transactions[0]['original_currency']=='USD'
    assert transactions[1]['original_currency'] in (None,'UNKNOWN')
    assert transactions[1]['original_amount']=='2860.00'
    assert 'CURRENCY_UNKNOWN' in {d['code'] for d in result['diagnostics']}
    assert transactions[0]['currency_source'] not in (None,'legacy_assumed')
    assert transactions[1]['currency_source'] not in (None,'legacy_assumed')


def test_column_reordering_cannot_swap_original_and_settlement_amount(mail_store):
    indices=[3,7,8,4,1,2,6,5,0]
    headers=[HEADER[i] for i in indices]
    rows=[[row[i] for i in indices] for row in ROWS]
    _,transactions=parse_case(mail_store,headers,rows)
    assert len(transactions)==2
    assert [(t['amount'],t['original_amount']) for t in transactions]==[('100.00','20.00'),('121.93','2860.00')]
    assert transactions[0]['description']=='FOREIGN SERVICE'
    assert transactions[1]['txn_location_code']=='JP'


def test_summary_amount_and_advertisement_changes_are_not_structural_drift(mail_store):
    # Existing current-case header (without newly added original-currency column).
    headers=HEADER[:-1]
    rows=[row[:-1] for row in ROWS]
    first,_=parse_case(mail_store,headers,rows,total='221.93')
    second,_=parse_case(mail_store,headers,rows,total='300.00',
        extra='<table><tr><td>活动</td><td>日期</td></tr><tr><td>广告内容</td><td>2026-11</td></tr></table>')
    for result in (first,second):
        assert 'FORMAT_CHANGED' not in {d['code'] for d in result['diagnostics']}


def test_zero_is_present_for_purpose_requirements(parser_repo):
    quality=importlib.import_module('mail_quality')
    rule={'quality':{'required_fields_by_purpose':{'financial_summary':['total_due']}}}
    assert quality.missing_fields_by_purpose(rule,{'total_due':{'value':0,'source':'extracted'}})=={}


def test_invalid_extracted_date_keeps_failed_conversion_evidence(mail_store):
    _, _, _, parser, _ = mail_store
    rule={'rule_id':'TEST','extract_rules':{'statement_fields':{
        'due_date':{'pattern':r'到期还款日：(\S+)'}}},
        'normalize_rules':{'date_formats':['%Y/%m/%d']}}
    fields,meta=parser.extract_statement_by_rule(rule,'','到期还款日：2026/99/99',return_meta=True)
    assert fields['due_date'] is None
    assert meta['due_date']['source']=='extraction_failed'
    assert '2026/99/99' in json.dumps(meta['due_date'],ensure_ascii=False)


def test_trends_require_both_currency_sources_and_disclose_partial_coverage(mail_store):
    db, sd, models, _, rm = mail_store
    txns=[models.StatementTransactionRecord(uid='sample',bank_code='CMB',description='GOOGLE*CHATGPT',
        amount='121.93',currency='CNY',currency_source=source,direction='debit',
        original_amount='2860.00',original_currency='JPY',original_currency_source=None)
        for source in (None,'legacy_assumed','extracted')]
    sd.save_statement_with_transactions(db,stored_statement(models),txns)
    result=rm.trends(db,months=6,merchant='GOOGLE*CHATGPT')
    assert len(result['excluded_transactions'])==2
    assert result['merchants'][0]['months'][0]['currencies'][0]['net']=='121.93'
    assert result['merchants'][0]['avg_fx_rate_sample'] is None
    assert not result['complete'] and not result['fx_complete']
    assert len(result['fx_excluded_transactions'])==1


def test_missing_detail_date_retains_good_details_but_blocks_trend_in_both_readers(mail_store):
    first,_=parse_case(mail_store,HEADER[:-1],[r[:-1] for r in ROWS])
    indices=[i for i in range(len(HEADER)-1) if i!=1]
    result,txns=parse_case(mail_store,[HEADER[i] for i in indices],[[r[i] for i in indices] for r in ROWS])
    assert len(txns)==2 and txns[0]['amount']=='100.00'
    assert {'FORMAT_CHANGED','TRANSACTION_COVERAGE_GAP'} <= {d['code'] for d in result['diagnostics']}
    db,_,_,_,rm=mail_store
    modern=rm.snapshot(db,months=6)
    legacy=build_snapshot(db,today=date(2026,10,11))
    for snapshot in (modern,legacy):
        assert not snapshot['transaction_totals']
        assert not snapshot['transaction_totals_complete']
        assert len(snapshot['gated_transactions'])==2
    trend=rm.trends(db,months=6)
    assert not trend['complete'] and not trend['merchants']


@pytest.mark.parametrize('currency',['USD/CNY','USDfoo','XYZ'])
def test_ambiguous_or_unsupported_currency_cannot_fall_back_to_header(mail_store,currency):
    row=list(ROWS[0]);row[5]=currency
    result,txns=parse_case(mail_store,HEADER,[row])
    assert txns[0]['currency']=='UNKNOWN'
    assert any(d['code']=='CURRENCY_UNKNOWN' and d['field']=='currency' for d in result['diagnostics'])


def test_invalid_original_amount_is_diagnosed_without_losing_settlement(mail_store):
    row=list(ROWS[0]);row[7]='NaN'
    result,txns=parse_case(mail_store,HEADER,[row])
    assert txns[0]['amount']=='100.00' and txns[0]['original_amount'] is None
    assert any(d['field']=='original_amount' and d['code']=='TRANSACTION_ROW_INVALID' for d in result['diagnostics'])
    db,_,_,_,rm=mail_store
    assert rm.trends(db,months=6)['merchants'][0]['avg_fx_rate_sample'] is None
    assert not rm.trends(db,months=6)['fx_complete']


def test_purpose_isolation_keeps_confirmed_reminder_despite_detail_gap(parser_repo):
    evidence=importlib.import_module('statement_evidence')
    _,_,models=parser_repo
    fields={f:{'value':v,'source':'extracted'} for f,v in
            [('due_date','2026-10-28'),('total_due','0'),('statement_month','2026-10')]}
    gates=evidence.purpose_availability([models.Diagnostic(code='TRANSACTION_COVERAGE_GAP',field='transactions',
        affected_purposes=['transaction_trends','fx_analysis'])],fields)
    assert gates['repayment_reminder']['status']=='available'
    assert gates['financial_summary']['status']=='available'
    assert gates['transaction_trends']['status']=='blocked'
    assert gates['fx_analysis']['status']=='blocked'


def case_sources(mail_store):
    db,sd,models,_,_=mail_store
    refs=[{'account':'test-account','message_id':f'<source{i}@sample.invalid>','uid':str(i),'content_hash':str(i)*64}
          for i in (1,2)]
    event=models.QualityEventRecord(case_key='r6-shared-field',event_type='quality_detected',
        account_name='test-account',bank_code='CMB',template_version='v1',
        diagnostics=[models.Diagnostic(code='EXTRACTION_FAILED',field='due_date',reason='invalid date')],evidence_refs=refs)
    event_id=sd.upsert_quality_event(db,event)
    return db,sd,models,refs,event,event_id,sd.capture_quality_cases(db,'test-account','CMB')


def test_resolution_requires_all_sources_same_version_and_positive_field_proof(mail_store):
    db,sd,_,refs,_,event_id,captured=case_sources(mail_store)
    def confirm(ref,version,**kwargs):
        return sd.confirm_quality_sources(db,captured,ref,set(),'v1',version,True,**kwargs)
    assert confirm(refs[0],'parser-a')==[] # Absence of a diagnostic is insufficient.
    assert confirm(refs[0],'parser-a',verified_fields={'due_date'})[0]['status']=='source_verified'
    assert confirm(refs[1],'parser-b',verified_fields={'due_date'})[0]['status']=='source_verified'
    assert confirm(refs[0],'parser-b',verified_fields={'due_date'})[0]['status']=='resolved'
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT event_type FROM quality_events WHERE event_id=?',(event_id,)).fetchone()[0]=='quality_resolved'
        assert c.execute("SELECT COUNT(*) FROM quality_event_sources WHERE state='resolved' AND checked_parser_version='parser-b'").fetchone()[0]==2


def test_resolution_rejects_changed_content_failed_parse_and_stale_revision(mail_store):
    db,sd,_,refs,event,event_id,captured=case_sources(mail_store)
    assert not sd.confirm_quality_sources(db,captured,dict(refs[0],content_hash='changed'),set(),'v1','parser',True,{'due_date'})
    assert not sd.confirm_quality_sources(db,captured,refs[0],set(),'v1','parser',False,{'due_date'})
    assert not sd.confirm_quality_sources(db,captured,refs[0],set(),'v2','parser',True,{'due_date'})
    event.evidence_refs=[dict(refs[0],content_hash='changed')]
    sd.upsert_quality_event(db,event)
    result=sd.confirm_quality_sources(db,captured,refs[1],set(),'v1','parser',True,{'due_date'})
    assert result[0]['status']=='stale'
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT event_type FROM quality_events WHERE event_id=?',(event_id,)).fetchone()[0]!='quality_resolved'


def test_confirmation_failure_rolls_back_financial_save(mail_store,monkeypatch):
    db,sd,models,_,_=mail_store
    def fail(*args,**kwargs):
        raise RuntimeError('confirmation failure')
    monkeypatch.setattr(sd,'_confirm_quality_sources_locked',fail)
    with pytest.raises(RuntimeError,match='confirmation failure'):
        sd.save_statement_with_transactions(db,stored_statement(models),[],quality_confirmation={'results':[]})
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM statements').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM quality_events').fetchone()[0]==0


def test_old_schema_without_currency_source_is_unverified_and_model_samples_are_bounded(billing_db):
    # fixture imported separately via local pytest fixture below, not production DB.
    with sqlite3.connect(billing_db) as c:
        columns = [row[1] for row in c.execute('PRAGMA table_info(statement_transactions)')
                   if row[1] != 'currency_source']
        projection = ', '.join('"' + column.replace('"', '""') + '"' for column in columns)
        c.execute('CREATE TABLE legacy_transactions AS SELECT ' + projection +
                  ' FROM statement_transactions')
        c.execute('DROP TABLE statement_transactions')
        c.execute('ALTER TABLE legacy_transactions RENAME TO statement_transactions')
    snapshot=build_snapshot(billing_db,today=date(2026,10,11),detail_limit=1)
    assert not snapshot['transaction_totals']
    assert len(snapshot['unknown_currency_transactions'])==3
    evidence=model_evidence(snapshot)
    assert evidence['unknown_currency_transactions_count']==3
    assert len(evidence['unknown_currency_transactions'])==1
    assert 'raw_row_json' not in evidence['unknown_currency_transactions'][0]


from tests.test_billing_workflow import billing_db


def test_unpaid_query_does_not_merge_mailboxes_and_blocks_unverified_records(mail_store):
    db,sd,models,_,_=mail_store
    gates={p:{'status':'available','reasons':[]} for p in ('repayment_reminder','financial_summary','transaction_trends','fx_analysis')}
    for i in (1,2):
        s=stored_statement(models,uid=f'box{i}',raw_fields={'quality':{'purpose_availability':gates}})
        s.account_name=f'box{i}'
        sd.save_statement_with_transactions(db,s,[],balances=[models.StatementBalanceRecord(currency='USD',total_due=s.total_due,currency_source='extracted')])
    sd.save_statement_with_transactions(db,stored_statement(models,uid='unknown'),[])
    rows=sd.get_unpaid_statements(db)
    assert len(rows)==3
    assert len([r for r in rows if r['currency']=='USD' and r['repayment_availability']['status']=='available'])==2
    assert len([r for r in rows if r['repayment_availability']['status']=='blocked'])==1


@pytest.mark.parametrize('fixture,code',[
    ('cmb_due_source_missing.eml','SOURCE_FIELD_MISSING'),
    ('cmb_due_extraction_failed.eml','EXTRACTION_FAILED'),
    ('cmb_due_source_unknown.eml','FIELD_SOURCE_UNKNOWN')])
def test_source_diagnostics_use_sanitized_fixtures_through_real_entry(mail_store,parser_repo,fixture,code):
    import copy
    from email import policy
    from email.parser import BytesParser
    root,_,_=parser_repo
    db,_,_,parser,_=mail_store
    rules=copy.deepcopy(parser.load_rule_files())
    if fixture=='cmb_due_source_unknown.eml':
        for rule in rules:
            if rule['bank_code']=='CMB':
                rule['extract_rules']['statement_fields']['due_date'].pop('source_probe',None)
    msg=BytesParser(policy=policy.default).parsebytes((root/'tests/fixtures'/fixture).read_bytes())
    result=parser.validate_and_save_email_message(msg,'fixture',rules=rules,account_name='test-account')
    assert result['status']=='saved'
    assert any(d['code']==code and d['field']=='due_date' for d in result['diagnostics'])
    assert build_snapshot(db,today=date(2026,10,11))['statements'][0]['purpose_availability']['repayment_reminder']['status']=='blocked'


def test_drift_creates_todo_through_real_cli_and_delivery_is_idempotent(mail_store,parser_repo,tmp_path):
    from integrations.mail.client import MailCLI
    from integrations.mail.quality_todo import consume_quality_events
    indices=[i for i in range(len(HEADER)-1) if i!=1]
    parse_case(mail_store,[HEADER[i] for i in indices],[[r[i] for i in indices] for r in ROWS])
    db,_,_,_,_=mail_store
    root,_,_=parser_repo
    todo_db=str(tmp_path/'todo.db')
    cli=MailCLI(str(root),db_path=db)
    first=consume_quality_events(cli,todo_db)
    assert first['created']>=1 and first['failed']==0
    with sqlite3.connect(todo_db) as c:
        assert c.execute("SELECT COUNT(*) FROM todos WHERE description LIKE '%FORMAT_CHANGED%'").fetchone()[0]==1
    assert consume_quality_events(cli,todo_db)['total']==0


def test_semantic_reordering_is_not_drift_and_numeric_zero_is_valid(mail_store):
    indices=[3,7,4,1,2,6,5,0]
    result,_=parse_case(mail_store,[HEADER[i] for i in indices],[[ROWS[0][i] for i in indices]])
    assert 'FORMAT_CHANGED' not in {d['code'] for d in result['diagnostics']}
    from features.billing.snapshot import decimal_amount
    assert decimal_amount(0)==0


def test_real_reparse_resolves_only_verified_field_and_closes_its_todo(mail_store,parser_repo,tmp_path):
    import copy
    from integrations.mail.client import MailCLI
    from integrations.mail.quality_todo import consume_quality_events
    db,_,_,parser,_=mail_store
    root,_,_=parser_repo
    rules=copy.deepcopy(parser.load_rule_files())
    for rule in rules:
        if rule['bank_code']=='CMB':
            rule['extract_rules']['statement_fields']['due_date']['pattern']=r'NO_MATCH(\d+)'
    msg=message_with_table(HEADER[:-1],[r[:-1] for r in ROWS])
    initial=parser.validate_and_save_email_message(msg,'stable',rules=rules,account_name='test-account')
    assert any(d['code']=='EXTRACTION_FAILED' and d['field']=='due_date' for d in initial['diagnostics'])
    todos=str(tmp_path/'todo.db')
    cli=MailCLI(str(root),db_path=db)
    assert consume_quality_events(cli,todos)['created']>=1
    result=parser.validate_and_save_email_message(msg,'stable',rules=parser.load_rule_files(),account_name='test-account')
    assert any(r['status']=='resolved' for r in result['resolution_results'])
    stats=consume_quality_events(cli,todos)
    assert stats['resolved']==1 and stats['failed']==0
    with sqlite3.connect(todos) as c:
        assert c.execute("SELECT status FROM todos WHERE description LIKE '%EXTRACTION_FAILED%'").fetchone()[0]=='done'
        assert c.execute("SELECT COUNT(*) FROM todos WHERE status='pending'").fetchone()[0]>=1


def test_reminder_cli_never_claims_no_due_when_all_records_are_unverified(mail_store,monkeypatch,capsys):
    db,sd,models,_,_=mail_store
    sd.save_statement_with_transactions(db,stored_statement(models),[])
    client=importlib.import_module('mail_client')
    monkeypatch.setattr(client,'DB_PATH',db)
    client.due_soon_bank_bills(months=6,days=365)
    output=capsys.readouterr().out
    assert '无法核验' in output
    assert '✅ 没有临近' not in output
    assert ' RMB' not in output


def test_zero_and_credit_balances_are_not_repayment_obligations(mail_store):
    db,sd,models,_,_=mail_store
    for uid,total in [('zero','0'),('credit','-10')]:
        sd.save_statement_with_transactions(db,stored_statement(models,uid=uid,total=total),[])
    assert sd.get_unpaid_statements(db)==[]
