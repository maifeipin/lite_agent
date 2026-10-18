#!/usr/bin/env python3
"""邮件实时列表技能 - AI 工具 + /mail 直通指令 (薄入口)"""

import sys, os
from lite_agent.core.skill_engine import skill
from lite_agent.core.command_registry import slash_command
from lite_agent.features.mail_ops import query_service


@skill(
    name="mail_list",
    description="列出最近 N 封邮件的收件箱原始内容（主题/发件人/UID），不经过 LLM 摘要",
    params={
        "limit": {"type": "integer", "description": "获取数量 (默认10)", "default": 10},
        "account_name": {"type": "string", "description": "邮箱账户名 (可选，默认第一账户)", "default": None},
    },
    guest_ok=False,
)
def mail_list(limit: int = 10, account_name: str = None) -> str:
    return query_service.mail_list(limit=limit, account_name=account_name)


def _cmd_mail_list(agent, msg, args):
    return query_service.cmd_mail_list(agent, msg, args)


slash_command('/mail_list', category='邮件管理',
              description='查看收件箱最新邮件列表',
              show_in_dashboard=True, guest_ok=False)(_cmd_mail_list)
