#!/usr/bin/env python3
"""补跑失败邮件技能 - AI 工具 + /reprocess 直通指令 (薄入口)"""

from core.skill_engine import skill
from core.command_registry import slash_command
from features.mail_ops import query_service


@skill(
    name="mail_reprocess",
    description="重试失败邮件：重新连接邮箱拉取并 LLM 摘要处理 status=failed 且 retry<3 的邮件",
    params={},
    guest_ok=False,
)
def mail_reprocess() -> str:
    return query_service.mail_reprocess()


def _cmd_mail_reprocess(agent, msg, args):
    return query_service.cmd_mail_reprocess(agent, msg, args)


slash_command('/mail_reprocess', category='邮件管理',
              description='重新解析历史未分类/失败的银行账单邮件',
              show_in_dashboard=False, guest_ok=False)(_cmd_mail_reprocess)
