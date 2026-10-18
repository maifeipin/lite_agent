#!/usr/bin/env python3
"""邮件搜索技能 - /search [replytype] <keyword> (薄入口)"""

from lite_agent.core.skill_engine import skill
from lite_agent.core.command_registry import slash_command
from lite_agent.features.mail_ops import query_service


_do_search = query_service.do_search
_format_summary = query_service._format_summary
_format_full_markdown = query_service._format_full_markdown


@skill(
    name="mail_search",
    description="全文搜索邮件正文/主题/发件人(email_bodies库毫秒级)，支持replytype:0=摘要(默认),2=上传HedgeDoc",
    params={
        "keyword": {"type": "string", "description": "搜索关键词"},
        "replytype": {"type": "integer", "description": "返回格式:0=摘要(默认),2=完整Markdown上传HedgeDoc", "default": 0},
        "limit": {"type": "integer", "description": "最大条数(默认20,上限100)", "default": 20},
    },
    guest_ok=False,
)
def mail_search(keyword: str, replytype: int = 0, limit: int = 20) -> str:
    return query_service.mail_search(keyword=keyword, replytype=replytype, limit=limit)


def _cmd_mail_search(agent, msg, args):
    return query_service.cmd_mail_search(agent, msg, args)


slash_command('/mail_search', category='邮件管理',
              description='全文搜索邮件正文/主题/发件人',
              show_in_dashboard=False, guest_ok=False)(_cmd_mail_search)
