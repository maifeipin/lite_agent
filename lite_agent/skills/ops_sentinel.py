import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lite_agent.core.skill_engine import skill
from lite_agent.features.sys_ops import security_service

Sentinel = security_service.Sentinel


@skill(
    name='sentinel_scan',
    description="执行轻量级安全扫描 (ops_sentinel)，检测 SSH、用户越权、持久化后门、异常进程等。",
    params={
        'mode': {
            'type': 'string',
            'description': '扫描模式：quick 或 full (当前暂无区别，均执行完整检查)',
            'default': 'quick'
        },
        'update_baseline': {
            'type': 'boolean',
            'description': '是否更新基线快照（在系统维护或预期变更后更新）',
            'default': False
        }
    },
    tags=['security', 'sysadmin']
)
def sentinel_scan(mode: str = 'quick', update_baseline: bool = False) -> str:
    return security_service.sentinel_scan(mode=mode, update_baseline=update_baseline)


@skill(
    name='sentinel_baseline',
    description="管理安全基线 (Baseline)。允许操作如 'show' (查看) 或 'refresh' (强制刷新，接受所有当前状态为合法漂移)。",
    params={
        'action': {
            'type': 'string',
            'description': '操作类型: show, refresh',
            'default': 'show'
        },
        'confirm': {
            'type': 'string',
            'description': '刷新基线需传 YES 确认',
            'default': ''
        }
    },
    tags=['security', 'sysadmin']
)
def sentinel_baseline(action: str = 'show', confirm: str = '') -> str:
    return security_service.sentinel_baseline(action=action, confirm=confirm)


@skill(
    name='sentinel_findings',
    description="查询本地发现的入侵或漂移告警。",
    params={
        'hours': {
            'type': 'integer',
            'description': '查询过去多少小时内的告警',
            'default': 24
        },
        'min_severity': {
            'type': 'string',
            'description': '最低严重级别: medium 或 high',
            'default': 'medium'
        },
        'limit': {
            'type': 'integer',
            'description': '可选最大记录条数；默认返回时间与严重度范围内全部记录',
            'default': None
        }
    },
    tags=['security', 'sysadmin']
)
def sentinel_findings(hours: int = 24, min_severity: str = 'medium', limit: int = None) -> str:
    return security_service.sentinel_findings(hours=hours, min_severity=min_severity, limit=limit)
