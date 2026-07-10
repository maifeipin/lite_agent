#!/usr/bin/env python3
"""邮件搜索技能 - AI 工具 + /search 直通指令
调用 mail_client.py search 命令（POP3/Graph 双通道），不依赖 Meilisearch。
"""

import sys, os, subprocess
from core.skill_engine import skill

_SCRIPT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "mail-statement-parser"
)


def _run_search_cmd(keyword: str, limit: int = 20, account_name: str = None) -> str:
    """执行 mail_client.py search 命令（实时 POP3/Graph 检索）"""
    if not os.path.exists(os.path.join(_SCRIPT_DIR, "mail_client.py")):
        return "❌ 找不到邮件脚本。"

    cmd = [
        sys.executable,
        os.path.join(_SCRIPT_DIR, "mail_client.py"),
        "search", keyword, str(limit)
    ]
    if account_name:
        cmd.append(account_name)

    r = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60,
        cwd=_SCRIPT_DIR
    )
    output = r.stdout or r.stderr
    if not output.strip():
        return f"🔍 未找到包含 '{keyword}' 的邮件。"
    return output


@skill(
    name="mail_search",
    description="实时搜索邮件：按关键词检索邮箱并返回匹配的邮件主题/发件人/UID",
    params={
        "keyword": {"type": "string", "description": "搜索关键词"},
        "limit": {"type": "integer", "description": "返回上限 (默认20)", "default": 20},
        "account_name": {"type": "string", "description": "邮箱账户名 (可选)", "default": None},
    },
    guest_ok=False,
)
def mail_search(keyword: str, limit: int = 20, account_name: str = None) -> str:
    return _run_search_cmd(keyword, limit, account_name)
