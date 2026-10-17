"""
Mail Ops 邮件运维与摘要模块 (features.mail_ops).
管理邮件收取、多条件全文检索、LLM智能摘要与原始HTML安全清洗。
实现受控插件 register(context) 契约。
"""

from typing import Any

MAIL_DOMAIN_SPEC = {
    "pattern": r"(邮件|邮箱|收件箱|mail|inbox)",
    "default_tools": [
        "mail_list", "mail_headers",
        "mail_missed", "mail_search", "mail_stats",
    ],
    "explicit_intent_tools": [
        "mail_fetch_summaries", "mail_fetch_only", "mail_llm_enrich",
        "mail_view_original", "mail_reprocess", "backfill_bodies",
    ],
    "explicit_patterns": r"(拉取|抓取|重新处理|重新解析|回填|补全|ocr|识别|摘要)",
    "explicit_routes": [
        (r"仅拉取|只抓取|只拉取", ["mail_fetch_only"]),
        (r"抓取.*摘要|拉取.*摘要|摘要.*邮件|(?<!只)(?<!仅)抓取邮件|(?<!只)(?<!仅)拉取邮件",
         ["mail_fetch_summaries"]),
        (r"重新处理|重新解析", ["mail_reprocess"]),
        (r"回填|补全", ["backfill_bodies"]),
    ],
}


def register(context: Any) -> None:
    """注册 Mail Ops 插件到宿主上下文。"""
    from skills import (
        ops_mail_reader,
        ops_mail_list,
        ops_mail_search,
        ops_mail_stats,
        ops_mail_reprocess,
    )
    from core.skill_engine import _skill_registry

    from features.mail_ops import reader_service, query_service

    # 1. 注册邮件工具技能
    mail_tools = [
        "mail_fetch_summaries", "mail_fetch_only", "mail_llm_enrich",
        "mail_headers", "mail_missed", "mail_view_original", "backfill_bodies",
        "mail_list", "mail_search", "mail_stats", "mail_reprocess"
    ]
    for t_name in mail_tools:
        if t_name in _skill_registry:
            meta = _skill_registry[t_name]
            if t_name == "mail_llm_enrich":
                host_agent = context.agent
                def enrich_for_host(limit=30):
                    return reader_service.mail_llm_enrich(limit=limit, agent=host_agent)
                meta = dict(meta, func=enrich_for_host)
            context.register_skill(t_name, meta)

    # 定时入口不是模型工具；绑定当前宿主，保留邮件服务内部的高优推送。
    if hasattr(context, "register_job_handler"):
        def _mail_fetch_cron_handler(**kwargs):
            return reader_service.mail_fetch_cron(agent=context.agent, **kwargs)
        context.register_job_handler("ops_mail_reader::mail_fetch_cron", _mail_fetch_cron_handler)

    # 2. 注册邮件运维 Slash 指令
    context.register_command(
        "/mail_search",
        query_service.cmd_mail_search,
        category="邮件管理",
        description="全文搜索邮件正文/主题/发件人",
        show_in_dashboard=False,
        guest_ok=False,
    )
    context.register_command(
        "/mail_reprocess",
        query_service.cmd_mail_reprocess,
        category="邮件管理",
        description="重新处理账单解析失败的邮件 (可指定 --limit N --dry-run)",
        show_in_dashboard=True,
        guest_ok=False,
    )
    context.register_command(
        "/mail_list",
        query_service.cmd_mail_list,
        category="邮件管理",
        description="查看最近抓取的邮件列表 (可指定条数，如 `/mail_list 20`)",
        show_in_dashboard=True,
        guest_ok=False,
    )
    context.register_command(
        "/mail_stats",
        query_service.cmd_mail_stats,
        category="邮件管理",
        description="查看邮件入库统计概览 (总数/分类/解析状态/时间范围)",
        show_in_dashboard=True,
        guest_ok=False,
    )
    context.register_command(
        "/mail_fetch",
        lambda agent, msg, args: reader_service.mail_fetch_cron(agent=agent or getattr(context, "agent", None)),
        category="邮件管理",
        description="同步拉取邮件 + LLM评分 + 高优推送",
        show_in_dashboard=True,
        guest_ok=False,
    )
    context.register_command(
        "/mail_fetch_only",
        lambda agent, msg, args: reader_service.mail_fetch_only(int(args[0]) if args else 1),
        category="邮件管理",
        description="幂等拉取最近邮件入库 (不调 LLM)",
        show_in_dashboard=True,
        guest_ok=False,
    )
    context.register_command(
        "/mail_enrich",
        lambda agent, msg, args: reader_service.mail_llm_enrich(
            limit=int(args[0]) if args else 30,
            agent=agent or getattr(context, "agent", None)
        ),
        category="邮件管理",
        description="对未处理邮件进行 AI 摘要、分类与提取",
        show_in_dashboard=True,
        guest_ok=False,
    )
    context.register_command(
        "/mail_ok",
        lambda agent, msg, args: reader_service.mail_feedback_ok(int(args[0]) if args else 0),
        category="邮件管理",
        description="标记邮件已处理 (processed)",
        show_in_dashboard=False,
        guest_ok=False,
    )
    context.register_command(
        "/mail_noise",
        lambda agent, msg, args: reader_service.mail_feedback_noise(int(args[0]) if args else 0),
        category="邮件管理",
        description="将邮件标记为噪声并学习降噪规则",
        show_in_dashboard=False,
        guest_ok=False,
    )
    context.register_command(
        "/mail_unnoise",
        lambda agent, msg, args: reader_service.mail_delete_noise_rule(args[0]) if args else "❌ 用法：/mail_unnoise <ID或规则值>",
        category="邮件管理",
        description="删除降噪规则 (按 ID 反查或直接指定)",
        show_in_dashboard=False,
        guest_ok=False,
    )
    context.register_command(
        "/mail_noiselist",
        lambda agent, msg, args: reader_service.mail_list_noise_rules(),
        category="邮件管理",
        description="查看当前自定义降噪规则列表",
        show_in_dashboard=False,
        guest_ok=False,
    )
    context.register_command(
        "/mail_headers",
        lambda agent, msg, args: reader_service.mail_headers(limit=int(args[0]) if args else 15),
        category="邮件管理",
        description="查看最近邮件标题列表",
        show_in_dashboard=False,
        guest_ok=False,
    )
    context.register_command(
        "/mail_missed",
        lambda agent, msg, args: reader_service.mail_missed(limit=int(args[0]) if args else 15),
        category="邮件管理",
        description="查看最近7天可能错过的非高优邮件",
        show_in_dashboard=False,
        guest_ok=False,
    )
    context.register_command(
        "/mail_view",
        lambda agent, msg, args: reader_service.mail_view_original(account=args[0], uid=args[1]) if len(args) >= 2 else "❌ 用法：/mail_view <账户> <UID>",
        category="邮件管理",
        description="查看邮件原文 (HedgeDoc)",
        show_in_dashboard=False,
        guest_ok=False,
    )
    context.register_command(
        "/mail_backfill",
        lambda agent, msg, args: reader_service.backfill_bodies(),
        category="邮件管理",
        description="回填缺失的早期账单原文 (修复搜索查不到的问题)",
        show_in_dashboard=True,
        guest_ok=False,
    )

    # 3. 注册路由 (邮件原始 HTML 渲染)
    from features.mail_ops import email_adapter

    def _handle_email_route(http_handler: Any, parsed_url: Any):
        email_adapter.handle_email_html(http_handler, parsed_url)

    context.register_route("GET", "/api/v1/email/html", _handle_email_route)
    context.register_route("GET", "/api/v1/email-html", _handle_email_route)

    # 4. 注册邮件 RequestSelector 领域路由
    if hasattr(context, "register_domain"):
        context.register_domain("mail", MAIL_DOMAIN_SPEC)


__all__ = ["register", "MAIL_DOMAIN_SPEC"]
