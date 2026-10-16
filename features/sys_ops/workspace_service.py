"""
Sys Ops 安全沙箱与工作区代码执行服务 (features.sys_ops.workspace_service)。
从 skills/ops_workspace.py 迁入工作区隔离运行与安全探针审计逻辑。
"""
import sys
import os
import time
import subprocess
import socket

WORKSPACE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'workspace')


def ops_workspace_run(code: str, timeout: int = 30) -> str:
    """在安全工作区内写入并执行 Python 代码，返回执行结果。"""
    os.makedirs(WORKSPACE, exist_ok=True)
    ts = int(time.time() * 1000)
    script_path = os.path.join(WORKSPACE, f'task_{ts}.py')

    # 注入安全探针：拦截提权命令 (best-effort 减速带，非隔离边界)
    audit_code = r"""
import sys, os, re

_BAD = ('sudo', 'su', 'visudo', 'passwd', 'pkexec', 'doas', 'sg', 'newgrp')
# 仅在命令起始位置匹配 (行首 / 管道 / 与或 / 分号 / 换行 / 子shell / 反引号), 避免误伤 `cat /etc/passwd` 这类参数
_CMDPOS = re.compile(r'(?:^|[|&;\n(`])\s*(sudo|su|visudo|passwd|pkexec|doas|sg|newgrp)\b')


def _lite_agent_deny(hit):
    print(f"❌ [Security Sandbox] 拒绝执行特权命令: {hit}")
    sys.exit(126)


def _lite_agent_audit_hook(event, args):
    if event not in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"):
        return
    if event == "os.system":
        c = args[0]
    elif event in ("subprocess.Popen", "os.posix_spawn"):
        c = args[1]
    else:  # os.exec
        c = args[1] if len(args) > 1 else None
    if not c:
        return

    if isinstance(c, (bytes, bytearray)):
        c = c.decode("utf-8", "ignore")

    # 走 shell 解释的字符串命令 (os.system 恒经 shell; subprocess shell=True 传字符串): 整串按命令位扫描
    if isinstance(c, str):
        m = _CMDPOS.search(c)
        if m:
            _lite_agent_deny(m.group(1))
        return

    # 列表形式 argv
    try:
        cmd_list = [str(x) for x in c]
    except Exception:
        return
    if not cmd_list:
        return
    cmd0 = os.path.basename(cmd_list[0])
    if cmd0 in _BAD:
        _lite_agent_deny(cmd0)

    # 壳包裹: bash -c "<script>" 等, 抽取内嵌脚本按命令位扫描
    if cmd0 in ("bash", "sh", "zsh", "dash", "ash", "busybox"):
        for i, arg in enumerate(cmd_list):
            if arg in ("-c", "-lc", "-ic") and i + 1 < len(cmd_list):
                m = _CMDPOS.search(cmd_list[i + 1])
                if m:
                    _lite_agent_deny(m.group(1))

    # env 包装器: env [VAR=..] <cmd> ..
    if cmd0 == "env":
        for tok in cmd_list[1:]:
            if "=" in tok or tok.startswith("-"):
                continue
            if os.path.basename(tok) in _BAD:
                _lite_agent_deny(os.path.basename(tok))
            break


sys.addaudithook(_lite_agent_audit_hook)
"""
    with open(script_path, 'w', encoding='utf-8') as f:
        f.write(audit_code + "\n" + code)

    try:
        r = subprocess.run(
            [sys.executable, script_path],
            capture_output=True, text=True, timeout=timeout,
            cwd=WORKSPACE
        )
        output = r.stdout.strip()
        if r.stderr.strip():
            output += '\n\n[stderr]:\n' + r.stderr.strip()

        try:
            hostname = socket.gethostname()
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("223.5.5.5", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            hostname, ip = "unknown", "unknown"

        header = f"=== 机器指纹: {hostname} ({ip}) ===\n"
        return header + (output or f'(无输出, 退出码: {r.returncode})')
    except subprocess.TimeoutExpired:
        return f'执行超时 ({timeout}s)，已强制终止'
    except Exception as e:
        return f'执行失败: {e}'
