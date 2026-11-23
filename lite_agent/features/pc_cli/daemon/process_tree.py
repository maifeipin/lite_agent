"""
跨平台进程树强杀与子进程启动参数工具 (daemon.process_tree).
借鉴 tarobird 实践：
- Windows 使用 CREATE_NO_WINDOW 启动，中止时调用 taskkill /PID <pid> /T /F 彻底清理子进程树。
- macOS / Linux 使用 start_new_session=True 独立进程组，中止时先 SIGTERM 宽限后 SIGKILL。
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from typing import Any, Dict


def build_subprocess_kwargs(cwd: str, env: Dict[str, str]) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "limit": 8 * 1024 * 1024,  # 8MB 单行缓冲，防止大段工具输出触发 LimitOverrunError
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        kwargs["start_new_session"] = True
    return kwargs


async def kill_process_tree(proc: asyncio.subprocess.Process, grace_sec: float = 3.0) -> None:
    """安全且彻底地终止子进程及其所有派生子进程。"""
    if proc.returncode is not None:
        return
    pid = proc.pid
    if not pid:
        return

    if sys.platform == "win32":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
            await asyncio.wait_for(killer.wait(), timeout=5.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return

    # POSIX (macOS / Linux)
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_sec)
    except asyncio.TimeoutError:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
