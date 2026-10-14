import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from channels.api import ApiHandler
from features.billing.snapshot import model_evidence, render_report
from core.execution_budget import estimate_input_tokens
from scripts import debug_api


def test_compact_evidence_is_lossless_for_gates_and_does_not_mutate_report():
    reason={'code':'CURRENCY_UNKNOWN','field':'currency','reason':'可靠币种证据不足。'*150}
    gates={p:{'status':'partial','reasons':[reason]} for p in ('financial_summary','transaction_trends','fx_analysis')}
    txn={'id':1,'statement_id':3,'account_name':'test','currency':'UNKNOWN','original_amount':'2860.00'}
    snapshot={'statements':[{'id':i,'total_due':'0','purpose_availability':gates,'balances':[{'currency':'UNKNOWN'}]} for i in range(20)],
              'transactions':[txn], 'unknown_currency_transactions':[txn], 'gated_transactions':[], 'transaction_count':1}
    original=copy.deepcopy(snapshot)
    evidence=model_evidence(snapshot)
    assert snapshot==original
    assert len(evidence['quality_reason_catalog'])==1
    for before,after in zip(snapshot['statements'],evidence['statements']):
        for purpose,gate in after['purpose_availability'].items():
            assert gate['status']==before['purpose_availability'][purpose]['status']
            assert [evidence['quality_reason_catalog'][r['ref']] for r in gate['reasons']]==before['purpose_availability'][purpose]['reasons']
    assert evidence['transaction_examples']==[]
    assert evidence['transaction_example_refs']==[1]
    assert evidence['unknown_currency_transactions'][0]['statement_id']==3
    assert render_report(snapshot)==render_report(original)
    compact=estimate_input_tokens([{'role':'user','content':json.dumps(evidence,ensure_ascii=False)}])
    raw=estimate_input_tokens([{'role':'user','content':json.dumps(snapshot,ensure_ascii=False)}])
    assert compact<raw/3


@pytest.mark.parametrize('status',['succeeded','partial','failed'])
def test_api_stream_stops_on_workflow_terminal_without_polling(monkeypatch,status):
    handler=ApiHandler.__new__(ApiHandler)
    handler.is_guest=False
    handler.wfile=io.BytesIO()
    handler.send_response=lambda _:None
    handler._send_cors_headers=lambda:None
    handler.send_header=lambda *args:None
    handler.end_headers=lambda:None
    loader=Mock(return_value=(json.dumps({'final_result':'test result'}),status))
    handler.server=SimpleNamespace(api_server=SimpleNamespace(agent=SimpleNamespace(session_mgr=SimpleNamespace(load_subtask_dag=loader))))
    monkeypatch.setattr('time.sleep',lambda _:pytest.fail('terminal task must not sleep or poll'))
    handler._handle_task_stream('task_id=test&session_id=test')
    assert loader.call_count==1
    payload=json.loads(handler.wfile.getvalue().decode().strip()[6:])
    assert payload['status']==status and payload['response']=='test result'


@pytest.mark.parametrize('status',['succeeded','partial'])
def test_debug_client_prints_terminal_response(monkeypatch,capsys,status):
    data='data: '+json.dumps({'status':status,'response':'complete report'})+'\n\n'
    monkeypatch.setattr(debug_api.urllib.request,'urlopen',lambda request:io.BytesIO(data.encode()))
    debug_api.stream_task(8887,'','task')
    output=capsys.readouterr().out
    assert 'Task '+status.upper() in output and 'complete report' in output
