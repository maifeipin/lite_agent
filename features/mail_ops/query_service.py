"""
Mail Ops 邮件查询与统计服务 (features.mail_ops.query_service)。
整合收件箱实时读取、全文检索、分类统计与解析重试。
"""
import sys
import os
import subprocess
from datetime import datetime
from typing import Optional, List, Dict, Any

from core.utils.hedgedoc import upload_to_hedgedoc
from integrations.mail.paths import get_statement_db_path, get_parser_script_dir
from integrations.mail.client import MailCLI, MailCLIError


def get_parser_dir() -> str:
    return get_parser_script_dir()


def mail_list(limit: int = 10, account_name: Optional[str] = None) -> str:
    """执行 mail_client.py read 命令（实时 POP3/Graph 拉取）"""
    script_dir = get_parser_dir()
    mail_client_py = os.path.join(script_dir, "mail_client.py")
    if not os.path.exists(mail_client_py):
        return "❌ 找不到邮件脚本。"

    cmd = [sys.executable, mail_client_py, "read", str(limit)]
    if account_name:
        cmd.append(account_name)

    r = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60,
        cwd=script_dir
    )
    output = r.stdout or r.stderr
    if not output.strip():
        return "📭 邮箱为空或无匹配。"
    return output


def _format_summary(keyword: str, rows: List[Dict[str, Any]]) -> str:
    """默认:截断摘要(每封 80 字符 snippet)"""
    if not rows:
        return f"🔍 未找到包含 '{keyword}' 的邮件。"
    lines = [f"🔍 搜索 '{keyword}' 找到 {len(rows)} 封:"]
    for r in rows:
        snippet = (r["plain_text"] or "")[:80].replace("\n", " ")
        lines.append(
            f"\n[{r['category']}/{r['importance']}] {r['subject'][:50]}\n"
            f"  发件人: {r['sender']}\n"
            f"  摘录: {snippet}"
        )
    return "\n".join(lines)


def _format_full_markdown(keyword: str, rows: List[Dict[str, Any]]) -> str:
    """完整 Markdown(上传 HedgeDoc 用)"""
    lines = [
        f"# 邮件搜索: {keyword}",
        f"\n> 共 {len(rows)} 封 · {datetime.now().isoformat()}",
        "\n---",
    ]
    for r in rows:
        lines.append(
            f"\n## [{r['category']}/{r['importance']}] {r['subject']}\n"
            f"- **发件人**: {r['sender']}\n"
            f"- **收件人**: {r['recipient']}\n"
            f"- **日期**: {r['email_date']}\n"
            f"- **账户**: {r['account_name']}\n"
            f"\n### 摘要\n{r['summary'] or '(无摘要)'}\n"
            f"\n### 正文\n```\n{(r['plain_text'] or '')[:2000]}\n```\n"
            f"\n---"
        )
    return "\n".join(lines)


def do_search(
    keyword: str,
    limit: int = 20,
    account_name: Optional[str] = None,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """查 email_bodies JOIN email_summaries，通过 MailCLI 统一接口"""
    try:
        limit_val = int(limit)
        if limit_val <= 0:
            limit_val = 20
        elif limit_val > 100:
            limit_val = 100
    except (ValueError, TypeError):
        limit_val = 20

    cli = MailCLI(db_path=db_path)
    try:
        return cli.mail_search(keyword=keyword, limit=limit_val, account_name=account_name)
    except Exception:
        return []


def mail_search(
    keyword: str,
    replytype: int = 0,
    limit: int = 20,
    account_name: Optional[str] = None,
    db_path: Optional[str] = None,
) -> str:
    """查 email_bodies JOIN email_summaries，通过 MailCLI 统一接口"""
    if not keyword or not keyword.strip():
        return "❌ 请提供搜索关键词。例如: /search 账单 或 /search 2 验证码"

    keyword = keyword.strip()
    rows = do_search(keyword, limit=limit, account_name=account_name, db_path=db_path)

    if replytype == 2:
        md = _format_full_markdown(keyword, rows)
        from core.config_loader import load_config
        cfg = load_config() or {}
        hc = cfg.get("hedgedoc", {})
        if not hc.get("enabled"):
            return "❌ HedgeDoc 未启用,请使用普通搜索(replytype=0)。"
        url = upload_to_hedgedoc(md, hc)
        if url:
            return f"📝 搜索 '{keyword}' 找到 {len(rows)} 封,完整结果已上传:\n👉 {url}"
        return "❌ 上传 HedgeDoc 失败,回退到普通模式:\n" + _format_summary(keyword, rows)

    return _format_summary(keyword, rows)


def mail_stats(db_path: Optional[str] = None) -> str:
    """统计 email_summaries 表的分类占比 / 重要性分布 / 处理成功率"""
    cli = MailCLI(db_path=db_path)
    try:
        data = cli.mail_stats()
    except MailCLIError as exc:
        err_msg = str(exc)
        if "邮件数据库不存在" in err_msg or "unable to open database file" in err_msg:
            return f"❌ 邮件数据库不存在: {db_path or cli.db_path}"
        return f"❌ 获取邮件统计失败: {exc}"
    except Exception as exc:
        return f"❌ 邮件统计执行异常: {exc}"

    total = data.get("total", 0)
    if total == 0:
        return "📭 暂无邮件记录。"

    cat_rows = data.get("categories", [])
    imp_rows = data.get("importance", [])
    status_map = data.get("statuses", {})
    acct_rows = data.get("accounts", [])

    processed = status_map.get("processed", 0)
    failed = status_map.get("failed", 0)
    pending = status_map.get("pending", 0)
    noise = status_map.get("noise", 0)

    lines = [
        f"📊 邮件统计 (共 {total} 封)",
        "",
        "### 📂 分类分布",
    ]
    for r in cat_rows:
        pct = r["cnt"] / total * 100
        lines.append(f"  {r['category']}: {r['cnt']} ({pct:.1f}%)")

    lines.append("")
    lines.append("### ⚡ 重要性分布")
    for r in imp_rows:
        pct = r["cnt"] / total * 100
        lines.append(f"  {r['importance']}: {r['cnt']} ({pct:.1f}%)")

    lines.append("")
    lines.append("### 📬 处理状态")
    lines.append(f"  ✅ 已处理: {processed}")
    lines.append(f"  🔕 已降噪: {noise}")
    lines.append(f"  ⏳ 待处理: {pending}")
    lines.append(f"  ❌ 失败: {failed}")
    if total > 0:
        success_rate = data.get("success_rate", (processed + noise) / total * 100)
        lines.append(f"  📈 成功率: {success_rate:.1f}%")

    if acct_rows:
        lines.append("")
        lines.append("### 👤 账户分布")
        for r in acct_rows:
            pct = r["cnt"] / total * 100
            lines.append(f"  {r['account_name']}: {r['cnt']} ({pct:.1f}%)")

    return "\n".join(lines)


def mail_reprocess() -> str:
    """重试失败邮件：重新连接邮箱拉取并 LLM 摘要处理 status=failed 且 retry<3 的邮件"""
    script_dir = get_parser_dir()
    mail_client_py = os.path.join(script_dir, "mail_client.py")
    if not os.path.exists(mail_client_py):
        return "❌ 找不到邮件脚本，请确认 mail-statement-parser 已部署到项目同级目录。"

    env = os.environ.copy()
    from core.config_loader import load_config
    from features.mail_ops.reader_service import build_llm_pool_env
    cfg = load_config() or {}
    if not all(env.get(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_PROVIDER", "LLM_MODEL")):
        llm = cfg.get("llm", {})
        env["LLM_API_KEY"] = llm.get("api_key", "")
        env["LLM_BASE_URL"] = llm.get("base_url", "https://api.openai.com/v1")
        env["LLM_PROVIDER"] = llm.get("provider", "openai")
        env["LLM_MODEL"] = llm.get("model", "gpt-4o-mini")
    env.update(build_llm_pool_env(cfg))

    r = subprocess.run(
        [sys.executable, mail_client_py, "reprocess_failed"],
        capture_output=True, text=True, timeout=300,
        cwd=script_dir, env=env
    )
    return r.stdout if r.returncode == 0 else f"❌ 补跑失败 (code={r.returncode}):\n{r.stderr}"


def cmd_mail_list(agent: Any, msg: Any, args: list) -> str:
    limit = 10
    account = None
    for a in args:
        if a.isdigit():
            limit = int(a)
        else:
            account = a
    return mail_list(limit=limit, account_name=account)


def cmd_mail_stats(agent: Any, msg: Any, args: list) -> str:
    return mail_stats()


def cmd_mail_reprocess(agent: Any, msg: Any, args: list) -> str:
    return mail_reprocess()


def cmd_mail_search(agent: Any, msg: Any, args: list) -> str:
    keyword = args[0] if args else ''
    replytype = int(args[1]) if len(args) > 1 else 0
    limit = int(args[2]) if len(args) > 2 else 20
    return mail_search(keyword=keyword, replytype=replytype, limit=limit)
