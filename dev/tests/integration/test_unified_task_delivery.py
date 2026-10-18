from unittest.mock import Mock,MagicMock,patch
import pytest
from lite_agent.core.task_result import TaskResult
from lite_agent.core.output_delivery import prepare_channel_output
from lite_agent.features.billing.workflow import BillingWorkflow


@pytest.mark.parametrize('mode',['','auto'])
def test_short_auto_reply_stays_complete_in_original_channel(tmp_path,mode):
    result=TaskResult('succeeded','完整短回复',output_policy={'full_delivery':mode} if mode else {})
    cfg={'output_delivery':{'channel_limits':{'wechat':500},'sqlite':{'path':str(tmp_path/'archive.db')}}}
    with patch('lite_agent.core.output_delivery._send_email') as send,patch('lite_agent.core.utils.hedgedoc.upload_to_hedgedoc') as upload:
        text=prepare_channel_output(result.content,'wechat',cfg,task_result=result)
    assert '完整短回复' in text and result.delivery['destination']=='inline'
    send.assert_not_called();upload.assert_not_called()


def test_explicit_email_for_short_result_is_sent_only_once(tmp_path):
    result=TaskResult('succeeded','short',output_policy={'full_delivery':'email'})
    cfg={'output_delivery':{'sqlite':{'path':str(tmp_path/'archive.db')}}}
    with patch('lite_agent.core.output_delivery._send_email',return_value='configured') as send:
        prepare_channel_output(result.content,'wechat',cfg,task_result=result)
        prepare_channel_output(result.content,'wechat',cfg,task_result=result)
    assert send.call_count==1 and result.delivery['status']=='submitted'


def test_explicit_hedgedoc_failure_never_sends_email_and_is_partial(tmp_path):
    result=TaskResult('succeeded','short',output_policy={'full_delivery':'hedgedoc'})
    cfg={'hedgedoc':{'enabled':True},'output_delivery':{'sqlite':{'path':str(tmp_path/'archive.db')}}}
    with patch('lite_agent.core.output_delivery._send_email') as send,patch('lite_agent.core.utils.hedgedoc.upload_to_hedgedoc',return_value='') as upload:
        prepare_channel_output(result.content,'wechat',cfg,task_result=result)
        prepare_channel_output(result.content,'wechat',cfg,task_result=result)
    send.assert_not_called();assert upload.call_count==1
    assert result.status=='partial' and result.delivery['destination']=='sqlite'


from dev.tests.features.test_billing_workflow import billing_db


def test_workflow_generates_only_then_shared_entry_archives_and_sends(billing_db,tmp_path):
    cfg={'llm':{'default':'glm','models':{'glm':{}}},'billing':{'statement_db_path':str(billing_db)},
         'output_delivery':{'sqlite':{'path':str(tmp_path/'archive.db')}}}
    router=MagicMock()
    router.get_call_profile.return_value={'max_tokens':2048,'timeout':60,'invoke_kwargs':{}}
    router.get_invoker.return_value.invoke_sync.return_value={'content':'analysis','usage_total':50,'finish_reason':'stop'}
    workflow=BillingWorkflow(cfg,router)
    with patch.object(workflow,'fetch',return_value=('',[])),patch('lite_agent.core.output_delivery._send_email',return_value='configured') as send,patch('lite_agent.core.output_delivery.archive_output',return_value='archive') as archive:
        result=workflow.run('拉取最新账单分析到邮箱','test')
        send.assert_not_called();archive.assert_not_called()
        assert result.output_policy['full_delivery']=='email' and not result.delivery.get('destination')
        prepare_channel_output(result.content,'wechat',cfg,task_result=result)
        assert send.call_count==1 and archive.call_count==1
        assert result.delivery['status']=='submitted'
