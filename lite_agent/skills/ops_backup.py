"""
Skills Ops Backup (skills.ops_backup).
备份与云同步模型工具入口（保留 @skill 契约与薄委托）。
业务与协议主体已迁至 canonical 服务 features/sys_ops/backup_service.py。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lite_agent.core.skill_engine import skill
from lite_agent.features.sys_ops import backup_service
from lite_agent.features.sys_ops.backup_service import (
    do_backup,
    do_backup_and_sync,
    _bdpan_sync_dir,
    _backup_vaultwarden,
    _backup_mongodb,
    _backup_halo,
    _backup_hedgedoc,
)


@skill(
    name='ops_backup_data',
    description='手动执行数据备份，包含聊天记录、邮件账单、Vaultwarden密码库、HedgeDoc数据库和附件。'
)
def ops_backup_data() -> str:
    """薄工具入口：委托调用 canonical 备份服务"""
    return backup_service.do_backup()


@skill(
    name='ops_backup_cloud',
    description='执行数据备份并同步到百度网盘，包含聊天记录、账单、Vaultwarden密码库、HedgeDoc数据库和附件。'
)
def ops_backup_cloud() -> str:
    """薄工具入口：委托调用 canonical 备份与云同步服务"""
    return backup_service.do_backup_and_sync()


# 向后兼容别名与导出
backup_data_cron = ops_backup_data
backup_cloud_cron = ops_backup_cloud

__all__ = [
    "ops_backup_data",
    "ops_backup_cloud",
    "do_backup",
    "do_backup_and_sync",
    "backup_data_cron",
    "backup_cloud_cron",
    "_bdpan_sync_dir",
]
