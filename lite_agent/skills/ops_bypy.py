import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lite_agent.core.skill_engine import skill
from lite_agent.features.sys_ops.bypy_service import (
    run_bypy_cmd,
    bypy_info as _bypy_info,
    bypy_mkdir as _bypy_mkdir,
    bypy_syncup as _bypy_syncup,
    bypy_syncdown as _bypy_syncdown,
)

_run_bypy_cmd = run_bypy_cmd

@skill(
    name='bypy_info',
    description='查看百度网盘的配额和当前使用情况'
)
def bypy_info() -> str:
    return _bypy_info()

@skill(
    name='bypy_mkdir',
    description='在百度网盘上创建目录',
    params={
        'remote_dir': {
            'type': 'string',
            'description': '要创建的网盘目录路径，例如 lite_agent'
        }
    }
)
def bypy_mkdir(remote_dir: str) -> str:
    return _bypy_mkdir(remote_dir=remote_dir)

@skill(
    name='bypy_syncup',
    description='将本地目录同步/备份到百度网盘',
    params={
        'local_dir': {
            'type': 'string',
            'description': '本地要备份的目录，例如 /root/.halo'
        },
        'remote_dir': {
            'type': 'string',
            'description': '百度网盘上的目标目录，例如 lite_agent/halo'
        }
    }
)
def bypy_syncup(local_dir: str, remote_dir: str) -> str:
    return _bypy_syncup(local_dir=local_dir, remote_dir=remote_dir)

@skill(
    name='bypy_syncdown',
    description='将百度网盘上的目录同步/下载到本地',
    params={
        'remote_dir': {
            'type': 'string',
            'description': '百度网盘上的目录，例如 lite_agent/halo'
        },
        'local_dir': {
            'type': 'string',
            'description': '本地目标目录，例如 /root/halo_restore'
        }
    }
)
def bypy_syncdown(remote_dir: str, local_dir: str) -> str:
    return _bypy_syncdown(remote_dir=remote_dir, local_dir=local_dir)
