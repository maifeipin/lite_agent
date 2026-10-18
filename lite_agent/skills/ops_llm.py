import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lite_agent.core.skill_engine import skill
from lite_agent.core.command_registry import slash_command
from lite_agent.features.sys_ops.llm_ops_service import (
    check_deepseek_balance as _check_deepseek_balance,
    llm_check_balance as _llm_check_balance,
    llm_usage_report as _llm_usage_report,
)

check_deepseek_balance = _check_deepseek_balance

slash_command('/balance', category='系统',
              description='查询 DeepSeek 账户余额',
              show_in_dashboard=False, guest_ok=False)(
    lambda agent, msg, args: check_deepseek_balance())

@skill(
    name='llm_check_balance',
    description='查询当前 DeepSeek AI 大模型账户的剩余余额。当你想知道 API 费用消耗情况时使用此技能。'
)
def llm_check_balance() -> str:
    return _llm_check_balance()

@skill(
    name='llm_usage_report',
    description='统计并生成最近几天内的大模型 API 消耗量报表（区分不同的模型）。',
    params={
        'days': {
            'type': 'integer',
            'description': '回溯查询的天数，默认为 7 天。',
            'default': 7
        }
    }
)
def llm_usage_report(days: int = 7) -> str:
    return _llm_usage_report(days=days)
