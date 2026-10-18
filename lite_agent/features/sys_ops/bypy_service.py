"""
Sys Ops 百度网盘运维服务 (features.sys_ops.bypy_service)。
从 skills/ops_bypy.py 迁入 Baidu PCS CLI 调用逻辑。
"""
import subprocess
from typing import List


def run_bypy_cmd(args: List[str], timeout: int = 600) -> str:
    """Helper to run bypy commands and return output"""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.returncode == 0:
            return f"✅ 成功:\n{result.stdout}"
        else:
            return f"❌ 失败 (Exit Code {result.returncode}):\n{result.stderr}\n{result.stdout}"
    except subprocess.TimeoutExpired:
        return f"❌ 执行超时 ({timeout}s)"
    except Exception as e:
        return f"❌ 执行异常: {str(e)}"


def bypy_info() -> str:
    """查看百度网盘的配额和当前使用情况"""
    return run_bypy_cmd(["bypy", "info"], timeout=30)


def bypy_mkdir(remote_dir: str) -> str:
    """在百度网盘上创建目录"""
    return run_bypy_cmd(["bypy", "mkdir", remote_dir], timeout=60)


def bypy_syncup(local_dir: str, remote_dir: str) -> str:
    """将本地目录同步/备份到百度网盘"""
    return run_bypy_cmd(["bypy", "syncup", local_dir, remote_dir], timeout=3600)


def bypy_syncdown(remote_dir: str, local_dir: str) -> str:
    """将百度网盘上的目录同步/下载到本地"""
    return run_bypy_cmd(["bypy", "syncdown", remote_dir, local_dir], timeout=3600)
