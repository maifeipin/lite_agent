#!/usr/bin/env python3
"""邮件实时列表技能 - AI 工具 + /mail 直通指令
直接从邮箱拉取最新收件箱（不打 LLM 摘要），支持指定账户。
"""

import sys, os, subprocess
from core.skill_engine import skill
from core.command_registry import slash_command

_SCRIPT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "mail-statement-parser"
)


def _run_read_cmd(limit: int = 10, account_name: str = None) -> str:
    """执行 mail_client.py read 命令（实时 POP3/Graph 拉取）"""
    if not os.path.exists(os.path.join(_SCRIPT_DIR, "mail_client.py")):
        return "❌ 找不到邮件脚本。"

    cmd = [sys.executable, os.path.join(_SCRIPT_DIR, "mail_client.py"), "read", str(limit)]
    if account_name:
        cmd.append(account_name)

    r = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60,
        cwd=_SCRIPT_DIR
    )
    output = r.stdout or r.stderr
    if not output.strip():
        return "📭 邮箱为空或无匹配。"
    return output


@skill(
    name="mail_list",
    description="列出最近 N 封邮件的收件箱原始内容（主题/发件人/UID），不经过 LLM 摘要",
    params={
        "limit": {"type": "integer", "description": "获取数量 (默认10)", "default": 10},
        "account_name": {"type": "string", "description": "邮箱账户名 (可选，默认第一账户)", "default": None},
    },
    guest_ok=False,
)
def mail_list(limit: int = 10, account_name: str = None) -> str:
    return _run_read_cmd(limit, account_name)

def _cmd_mail_list(agent, msg, args):
    limit = 10
    account = None
    for a in args:
        if a.isdigit():
            limit = int(a)
        else:
            account = a
    return mail_list(limit=limit, account_name=account)

slash_command('/mail_list', category='邮件管理',
              description='查看收件箱最新邮件列表',
              show_in_dashboard=True, guest_ok=False)(_cmd_mail_list)
