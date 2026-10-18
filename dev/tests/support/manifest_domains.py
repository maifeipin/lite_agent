"""
测试环境业务领域路由汇总装配器 (tests.support.manifest_domains)。
用于在纯单元测试环境下聚合各 features 业务领域路由规则，填充至测试桩。
严禁在 production app 或 core 目录中导入此文件。
"""

from typing import Dict, Any


def get_all_manifest_domains() -> Dict[str, Dict[str, Any]]:
    """从各 features 模块加载并聚合全部业务领域路由描述供测试桩使用。"""
    from lite_agent.core.request_selector import CORE_DOMAIN_MAP
    from lite_agent.features.billing import BILLING_DOMAIN_SPEC
    from lite_agent.features.todo import TODO_DOMAIN_SPEC
    from lite_agent.features.review_meeting import REVIEW_MEETING_DOMAIN_SPEC, DECISION_DOMAIN_SPEC
    from lite_agent.features.mail_ops import MAIL_DOMAIN_SPEC
    from lite_agent.features.network_ops import SOCKS5_DOMAIN_SPEC, EDGE_DOMAIN_SPEC
    from lite_agent.features.sys_ops import SECURITY_DOMAIN_SPEC, BACKUP_DOMAIN_SPEC, WORKSPACE_DOMAIN_SPEC
    from lite_agent.features.content_ops import (
        WEB_DOMAIN_SPEC,
        BLOG_DOMAIN_SPEC,
        RSS_SEARCH_DOMAIN_SPEC,
        GAOKAO_DOMAIN_SPEC,
        MEDIA_DOMAIN_SPEC,
    )

    domains = dict(CORE_DOMAIN_MAP)
    domains.update({
        "mail": MAIL_DOMAIN_SPEC,
        "billing": BILLING_DOMAIN_SPEC,
        "todo": TODO_DOMAIN_SPEC,
        "socks5": SOCKS5_DOMAIN_SPEC,
        "edge": EDGE_DOMAIN_SPEC,
        "security": SECURITY_DOMAIN_SPEC,
        "workspace": WORKSPACE_DOMAIN_SPEC,
        "gaokao": GAOKAO_DOMAIN_SPEC,
        "web": WEB_DOMAIN_SPEC,
        "blog": BLOG_DOMAIN_SPEC,
        "backup": BACKUP_DOMAIN_SPEC,
        "media": MEDIA_DOMAIN_SPEC,
        "decision": DECISION_DOMAIN_SPEC,
        "review_meeting": REVIEW_MEETING_DOMAIN_SPEC,
        "rss_search": RSS_SEARCH_DOMAIN_SPEC,
    })
    return domains
