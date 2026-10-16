from unittest.mock import Mock, patch

import pytest
from core.task_orchestrator import TaskOrchestrator
from core.task_result import TaskResult
from features.billing.workflow import BillingWorkflow
from services.workflows import default_workflow_registry

if "billing" not in default_workflow_registry._workflows:
    default_workflow_registry.register(
        "billing",
        factory=lambda cfg, router, ledger: BillingWorkflow(cfg, router, ledger),
        matcher=lambda cfg, text: BillingWorkflow(cfg, None, None).matches(text),
    )


def orchestrator():
    orch=TaskOrchestrator.__new__(TaskOrchestrator)
    orch.config={'task_routing':{'billing_workflow':{'enabled':True}}}
    orch.router=Mock();orch.ledger=Mock();orch.session_mgr=Mock()
    orch.workflow_registry=default_workflow_registry
    return orch


@pytest.mark.parametrize('mode',['','inline','email','hedgedoc','sqlite'])
def test_generic_execute_uses_real_signature_and_excludes_delivery_option(mode):
    orch=orchestrator()
    def result(*args,**kwargs):
        orch.last_result=TaskResult('succeeded','completed')
        return 'completed'
    with patch.object(orch,'execute',autospec=True,side_effect=result) as execute:
        actual=orch.execute_result('分析问题并给出方案','session',task_id='task',step_override=3,output_mode=mode)
    assert actual.status=='succeeded'
    execute.assert_called_once_with('分析问题并给出方案','session',task_id='task',step_override=3)


@pytest.mark.parametrize('mode',['inline','email','hedgedoc','sqlite'])
def test_billing_branch_keeps_delivery_option(mode):
    orch=orchestrator()
    expected=TaskResult('succeeded','billing result')
    with patch('features.billing.workflow.BillingWorkflow.run',return_value=expected) as run, patch.object(orch,'execute',autospec=True) as execute:
        result=orch.execute_result('拉取最新账单并分析到邮箱','session',output_mode=mode,task_id='bill')
    run.assert_called_once_with('拉取最新账单并分析到邮箱','session',None,output_mode=mode)
    execute.assert_not_called()
    assert result is expected


def test_preplanned_billing_goal_also_uses_generic_signature():
    orch=orchestrator();planned=[{'id':'one'}]
    with patch.object(orch,'execute',autospec=True,return_value='result') as execute:
        result=orch.execute_result('拉取最新账单并分析到邮箱','session',planned_subtasks=planned,output_mode='inline')
    execute.assert_called_once_with('拉取最新账单并分析到邮箱','session',planned_subtasks=planned)
    assert result.content=='result'


@pytest.mark.parametrize('mode',['inline','email','hedgedoc','sqlite'])
def test_generic_channel_delivery_keeps_original_user_choice(mode):
    from agent import Agent,IncomingMessage
    agent=Agent.__new__(Agent);agent.channels=[]
    agent._prepare_output=Mock(return_value='prepared')
    message=IncomingMessage(channel='api',user_id='test',chat_id='test',message_id='one',text='分析',output_mode=mode)
    result=TaskResult('succeeded','result')
    agent._push_result(message,result)
    assert agent._prepare_output.call_args.kwargs['overrides']=={'full_delivery':mode}
    assert agent._prepare_output.call_args.kwargs['task_result'] is result
    assert message.output_mode==mode
