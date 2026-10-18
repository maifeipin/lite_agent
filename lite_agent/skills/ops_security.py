import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lite_agent.core.skill_engine import skill
from lite_agent.features.sys_ops.security_service import ops_security_audit as _ops_security_audit

@skill(
    name='ops_security_audit',
    description='安全审查：检查最近的失败登录尝试、SSH爆破情况及当前连接',
    params={
        'hours': {
            'type': 'integer',
            'description': '检查最近多少小时内的登录记录',
            'default': 24
        }
    }
)
def ops_security_audit(hours: int = 24) -> str:
    return _ops_security_audit(hours=hours)
