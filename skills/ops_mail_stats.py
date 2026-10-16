#!/usr/bin/env python3
"""邮件统计技能 - AI 工具 + /mailstats 直通指令 (薄入口)"""

from core.skill_engine import skill
from core.command_registry import slash_command
from features.mail_ops import query_service


_compute_stats = query_service.mail_stats


@skill(
    name="mail_stats",
    description="获取本地邮件库整体统计（总数、分类分布、重要性分布、处理成功率、账户分布）",
    params={},
    guest_ok=False,
)
def mail_stats() -> str:
    return query_service.mail_stats()


def _cmd_mail_stats(agent, msg, args):
    return query_service.cmd_mail_stats(agent, msg, args)


slash_command('/mail_stats', category='邮件管理',
              description='查看邮件入库统计概览',
              show_in_dashboard=True, guest_ok=False)(_cmd_mail_stats)
