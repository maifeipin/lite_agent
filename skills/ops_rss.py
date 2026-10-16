"""
RSS 技能入口 (skills/ops_rss.py)。
薄入口层，委托 canonical 服务 features.content_ops.rss_service。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.command_registry import slash_command
from features.content_ops import rss_service

# 兼容常量与配置导出
SITE_QUALITY = rss_service.SITE_QUALITY
BRIEF_GROUPS = rss_service.BRIEF_GROUPS
KEYWORD_WEIGHTS = rss_service.KEYWORD_WEIGHTS
V2EX_LOW_TAGS = rss_service.V2EX_LOW_TAGS
PUSHED_FILE = rss_service.PUSHED_FILE
CACHE_FILE = rss_service.CACHE_FILE

# 委托函数
_rss_config = rss_service.get_rss_config
_get_db = rss_service.get_rss_db
_v2ex_token = rss_service.get_v2ex_token
_clean_excerpt = rss_service.clean_excerpt
_keyword_score = rss_service.keyword_score
_v2ex_reply_count = rss_service.v2ex_reply_count
_rss_brief_compute = rss_service.rss_brief_compute
_detail_view = rss_service._detail_view
_list_view = rss_service._list_view
_overview_view = rss_service._overview_view
_meili_search = rss_service.meili_search

handle_rss = rss_service.handle_rss
rss_precompute = rss_service.rss_precompute
rss_brief = rss_service.rss_brief

_cmd_rss_list = rss_service.cmd_rss_list
_cmd_rss_log = rss_service.cmd_rss_log
_cmd_rss_topic = rss_service.cmd_rss_topic


slash_command('/rss_fetch', category='RSS',
              description='获取今日 RSS 精选摘要 (Top 5)',
              show_in_dashboard=False, guest_ok=False)(
    lambda agent, msg, args: rss_brief() or '(今日暂无精选文章)')


slash_command('/rss_list', category='RSS',
              description='查看 RSS 分组文章列表 (无参=概览, <分组>=列表)',
              show_in_dashboard=True, guest_ok=False)(_cmd_rss_list)


slash_command('/rss_log', category='RSS',
              description='查看 RSS 推送/预计算日志',
              show_in_dashboard=False, guest_ok=False)(_cmd_rss_log)


slash_command('/rss_topic', category='RSS',
              description='按分类/主题查 RSS (Meilisearch, 无参=标签概览)',
              show_in_dashboard=True, guest_ok=False)(_cmd_rss_topic)
