import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from features.sys_ops import health_service


@skill(
    name='ops_list_crontab',
    description='查看当前用户的 crontab 定时任务列表'
)
def ops_list_crontab() -> str:
    return health_service.ops_list_crontab()


@skill(
    name='ops_run_script',
    description='执行 VPS 上的指定脚本文件（如备份脚本、证书检查脚本等），返回执行输出。仅限 .sh 和 .py 脚本',
    params={
        'script_path': {
            'type': 'string',
            'description': '脚本的绝对路径，如 /root/script/backup.sh'
        },
        'args': {
            'type': 'string',
            'description': '传递给脚本的参数（可选）',
            'default': ''
        }
    }
)
def ops_run_script(script_path: str, args: str = '') -> str:
    return health_service.ops_run_script(script_path=script_path, args=args)
