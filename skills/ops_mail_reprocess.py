#!/usr/bin/env python3
"""补跑失败邮件技能 - AI 工具 + /reprocess 直通指令"""

import sys, os, subprocess
from core.skill_engine import skill

_SCRIPT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "mail-statement-parser"
)


def _run_reprocess_cmd() -> str:
    """执行 mail_client.py reprocess_failed 命令"""
    if not os.path.exists(os.path.join(_SCRIPT_DIR, "mail_client.py")):
        return "❌ 找不到邮件脚本，请确认 mail-statement-parser 已部署到项目同级目录。"

    env = os.environ.copy()
    for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_PROVIDER", "LLM_MODEL"):
        if not env.get(k):
            from core.config_loader import load_config
            cfg = load_config()
            llm = cfg.get("llm", {})
            env["LLM_API_KEY"] = llm.get("api_key", "")
            env["LLM_BASE_URL"] = llm.get("base_url", "https://api.openai.com/v1")
            env["LLM_PROVIDER"] = llm.get("provider", "openai")
            env["LLM_MODEL"] = llm.get("model", "gpt-4o-mini")
            break

    r = subprocess.run(
        [sys.executable, os.path.join(_SCRIPT_DIR, "mail_client.py"), "reprocess_failed"],
        capture_output=True, text=True, timeout=300,
        cwd=_SCRIPT_DIR, env=env
    )
    return r.stdout if r.returncode == 0 else f"❌ 补跑失败 (code={r.returncode}):\n{r.stderr}"


@skill(
    name="mail_reprocess",
    description="重试失败邮件：重新连接邮箱拉取并 LLM 摘要处理 status=failed 且 retry<3 的邮件",
    params={},
    guest_ok=False,
)
def mail_reprocess() -> str:
    return _run_reprocess_cmd()
