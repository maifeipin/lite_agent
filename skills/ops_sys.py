import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from features.sys_ops import health_service


@skill(
    name='ops_sys_status',
    description='获取VPS系统状态，包括主机名、运行时间、系统负载、CPU使用率、内存和磁盘信息',
    params={
        'detail': {
            'type': 'boolean',
            'description': '是否返回详细信息（包含占用内存和CPU最高的进程列表）',
            'default': False
        }
    }
)
def ops_sys_status(detail: bool = False) -> str:
    return health_service.ops_sys_status(detail=detail)
