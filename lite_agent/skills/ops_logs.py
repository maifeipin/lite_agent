import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lite_agent.core.skill_engine import skill
from lite_agent.features.sys_ops import health_service


@skill(
    name='ops_read_logs',
    description='读取 VPS 上指定的日志文件。可按关键字过滤',
    params={
        'log_path': {
            'type': 'string',
            'description': '日志文件的绝对路径，如 /var/log/syslog'
        },
        'keyword': {
            'type': 'string',
            'description': '可选的过滤关键字，只返回包含该关键字的行',
            'default': ''
        },
        'lines': {
            'type': 'integer',
            'description': '返回最近多少行日志',
            'default': 50
        }
    }
)
def ops_read_logs(log_path: str, keyword: str = '', lines: int = 50) -> str:
    return health_service.ops_read_logs(log_path=log_path, keyword=keyword, lines=lines)


@skill(
    name='ops_read_journal',
    description='使用 journalctl 查看指定 systemd 服务的日志，可按关键字过滤',
    params={
        'service_name': {
            'type': 'string',
            'description': 'systemd 服务名称，如 lite-agent、nginx 等',
            'default': ''
        },
        'keyword': {
            'type': 'string',
            'description': '可选的过滤关键字',
            'default': ''
        },
        'lines': {
            'type': 'integer',
            'description': '返回最近多少行日志',
            'default': 50
        },
        'unit': {
            'type': 'string',
            'description': 'systemd 服务名称（兼容别名）',
            'default': ''
        },
        'priority': {
            'type': 'string',
            'description': '日志优先级过滤：emerg, alert, crit, err, warning, notice, info, debug',
            'default': ''
        }
    }
)
def ops_read_journal(service_name: str = '', keyword: str = '', lines: int = 50, unit: str = '', priority: str = '') -> str:
    return health_service.ops_read_journal(service_name=service_name, keyword=keyword, lines=lines, unit=unit, priority=priority)
