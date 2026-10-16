"""
Skills Ops Meili Sync (skills.ops_meili_sync).
Meilisearch 同步模型工具与 Slash 指令薄入口（保留 @skill 契约与薄委托）。
业务与数据库主体位于 canonical 服务 features/content_ops/meili_sync_service.py。
"""

import sys, os
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from core.command_registry import slash_command
from features.content_ops import meili_sync_service
from features.content_ops.meili_sync_service import (
    sync_meili as _canonical_sync_meili,
    sync_meili_command as _canonical_sync_meili_command,
    _get_sync_state,
    _save_sync_state,
    _meili_request,
    _get_mongodb,
    pymongo,
)


@skill("sync_meili", "同步数据到 Meilisearch 搜索引擎")
def sync_meili() -> str:
    """薄工具入口：委托调用 canonical Meilisearch 同步服务"""
    return meili_sync_service.sync_meili()


def _cmd_sync_meili(agent, msg, args):
    """薄指令入口：委托调用 canonical 同步指令"""
    return meili_sync_service.sync_meili_command(agent, msg, args)


slash_command('/sync_meili', category='系统',
              description='增量同步邮件和 RSS 到 Meilisearch 搜索引擎',
              show_in_dashboard=True, guest_ok=False)(_cmd_sync_meili)

__all__ = [
    "sync_meili",
    "_cmd_sync_meili",
    "_get_sync_state",
    "_save_sync_state",
    "_meili_request",
    "_get_mongodb",
    "pymongo",
]
