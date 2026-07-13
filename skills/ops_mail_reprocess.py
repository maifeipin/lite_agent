#!/usr/bin/env python3
"""补跑失败邮件技能 - AI 工具 + /reprocess 直通指令"""

import sys, os, subprocess
from core.skill_engine import skill
from core.command_registry import slash_command

_SCRIPT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "mail-statement-parser"
)


def _run_reprocess_cmd() -> str:
    """执行 mail_client.py reprocess_failed 命令"""
    if not os.path.exists(os.path.join(_SCRIPT_DIR, "mail_client.py")):
        return "❌ 找不到邮件脚本，请确认 mail-statement-parser 已部署到项目同级目录。"

    env = os.environ.copy()
    from core.config_loader import load_config
    from skills.ops_mail_reader import _build_llm_pool_env
    cfg = load_config() or {}
    if not all(env.get(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_PROVIDER", "LLM_MODEL")):
        llm = cfg.get("llm", {})
        env["LLM_API_KEY"] = llm.get("api_key", "")
        env["LLM_BASE_URL"] = llm.get("base_url", "https://api.openai.com/v1")
        env["LLM_PROVIDER"] = llm.get("provider", "openai")
        env["LLM_MODEL"] = llm.get("model", "gpt-4o-mini")
    # LLM 端点池注入（配置了 llm.mail_pool 时启用 429 轮换）
    env.update(_build_llm_pool_env(cfg))

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

def _cmd_mail_reprocess(agent, msg, args):
    return mail_reprocess()

slash_command('/mail_reprocess', category='邮件管理',
              description='重新解析历史未分类/失败的银行账单邮件',
              show_in_dashboard=False, guest_ok=False)(_cmd_mail_reprocess)
