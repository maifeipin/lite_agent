"""
Content Ops 内容聚合与知识发布模块 (features.content_ops).
管理 RSS 订阅、网页剪藏、博客发布、Meilisearch 同步、媒体曲库、高考推荐与互联网搜索。
实现受控插件 register(context) 契约。
"""

from typing import Any

WEB_DOMAIN_SPEC = {
    "pattern": r"(网页|剪藏|全网搜|联网搜索|网上搜索|http://|https://|剪藏.*链接)",
    "default_tools": [
        "web_search", "web_clip", "ops_web_fetch",
    ],
    "explicit_intent_tools": [
        "hedgedoc_upload",
    ],
    "explicit_patterns": r"(上传到\s*hedgedoc)",
}

BLOG_DOMAIN_SPEC = {
    "pattern": r"(博客|halo)",
    "default_tools": [
        "blog_list_articles",
    ],
    "explicit_intent_tools": [
        "blog_export_articles",
        "blog_publish_article",
    ],
    "explicit_patterns": r"(发布|发表|更新文章|导出|导出文章)",
}

RSS_SEARCH_DOMAIN_SPEC = {
    "pattern": r"(RSS节点|RSS订阅|Meilisearch|Meili索引)",
    "default_tools": ["ops_rss_node_status"],
    "explicit_intent_tools": ["sync_meili"],
    "explicit_patterns": r"(同步|重建.*索引|更新.*索引)",
}

GAOKAO_DOMAIN_SPEC = {
    "pattern": r"(高考|位次|志愿|录取|院校|专业)",
    "default_tools": [
        "gaokao_score_rank", "gaokao_school_admission",
        "gaokao_major_admission", "gaokao_recommend",
        "gaokao_school_info", "gaokao_sql", "gaokao_stats",
    ],
    "explicit_intent_tools": [],
    "explicit_patterns": None,
}

MEDIA_DOMAIN_SPEC = {
    "pattern": r"(音乐|歌曲|媒体库|封面|重复歌曲)",
    "default_tools": [
        "ops_media_music_stats", "ops_media_music_duplicates",
    ],
    "explicit_intent_tools": [],
    "explicit_patterns": None,
}



def register(context: Any) -> None:
    """注册 Content Ops 插件到宿主上下文。"""
    from skills import (
        ops_rss_node,
        ops_web_clipper,
        ops_blog,
        ops_meili_sync,
        ops_media,
        ops_gaokao,
        ops_search,
        ops_workspace,
    )
    from core.skill_engine import _skill_registry
    from features.content_ops import rss_service

    # 1. 注册内容操作工具技能
    content_tools = [
        "ops_rss_node_status", "web_clip", "hedgedoc_upload", "blog_list_articles",
        "blog_publish_article", "blog_export_articles", "sync_meili",
        "ops_media_music_stats", "ops_media_music_duplicates", "gaokao_score_rank",
        "gaokao_school_admission", "gaokao_major_admission", "gaokao_recommend",
        "gaokao_school_info", "gaokao_sql", "gaokao_stats", "web_search",
        "ops_web_fetch"
    ]
    for t_name in content_tools:
        if t_name in _skill_registry:
            context.register_skill(t_name, _skill_registry[t_name])


    # 2. 注册 Slash 指令
    context.register_command(
        "/rss_fetch",
        rss_service.cmd_rss_fetch,
        category="RSS",
        description="获取今日 RSS 精选摘要 (Top 5)",
        show_in_dashboard=False,
        guest_ok=False,
    )
    context.register_command(
        "/rss_list",
        rss_service.cmd_rss_list,
        category="RSS",
        description="查看 RSS 分组文章列表 (无参=概览, <分组>=列表)",
        show_in_dashboard=True,
        guest_ok=False,
    )
    context.register_command(
        "/rss_log",
        rss_service.cmd_rss_log,
        category="RSS",
        description="查看 RSS 推送/预计算日志",
        show_in_dashboard=False,
        guest_ok=False,
    )
    context.register_command(
        "/rss_topic",
        rss_service.cmd_rss_topic,
        category="RSS",
        description="按分类/主题查 RSS (Meilisearch, 无参=标签概览)",
        show_in_dashboard=True,
        guest_ok=False,
    )
    from features.content_ops import meili_sync_service
    context.register_command(
        "/sync_meili",
        meili_sync_service.sync_meili_command,
        category="系统",

        description="增量同步邮件和 RSS 到 Meilisearch 搜索引擎",
        show_in_dashboard=True,
        guest_ok=False,
    )

    # 3. 注册路由
    from features.content_ops import rss_adapter

    def _handle_content_route(http_handler: Any, parsed_url: Any):
        req_path = parsed_url.path
        if req_path.startswith('/agent/'):
            req_path = req_path[6:]
        elif req_path == '/agent':
            req_path = '/'

        if req_path == "/api/v1/rss/brief":
            rss_adapter.handle_rss_brief(http_handler, parsed_url)
        elif req_path == "/api/v1/ocr":
            if hasattr(http_handler, "_handle_ocr_proxy"):
                http_handler._handle_ocr_proxy()
            else:
                http_handler.send_error(404, "Not Found")
        else:
            http_handler.send_error(404, "Not Found")

    context.register_route("GET", "/api/v1/rss/brief", _handle_content_route)
    context.register_route("POST", "/api/v1/ocr", _handle_content_route)

    # 4. 注册定时作业及专用回调处理器
    def _rss_push_handler(**kwargs):
        return rss_service.rss_push_job(context.send_owner_response)

    if hasattr(context, "register_job_handler"):
        context.register_job_handler("ops_rss::rss_precompute", lambda **kw: rss_service.rss_precompute())
        context.register_job_handler("ops_rss::rss_push", _rss_push_handler)

    # 5. 注册内容操作 RequestSelector 领域路由
    if hasattr(context, "register_domain"):
        context.register_domain("web", WEB_DOMAIN_SPEC)
        context.register_domain("blog", BLOG_DOMAIN_SPEC)
        context.register_domain("rss_search", RSS_SEARCH_DOMAIN_SPEC)
        context.register_domain("gaokao", GAOKAO_DOMAIN_SPEC)
        context.register_domain("media", MEDIA_DOMAIN_SPEC)


__all__ = [
    "register",
    "WEB_DOMAIN_SPEC",
    "BLOG_DOMAIN_SPEC",
    "RSS_SEARCH_DOMAIN_SPEC",
    "GAOKAO_DOMAIN_SPEC",
    "MEDIA_DOMAIN_SPEC",
]
