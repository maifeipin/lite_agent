#!/usr/bin/env python3
"""邮件统计技能 - AI 工具 + /mailstats 直通指令
统计 email_summaries 表的分类占比 / 重要性分布 / 处理成功率。
通过 MailCLI 消费只读统计，不直接操作外仓数据库或编写 SQL。
"""

from core.skill_engine import skill
from core.command_registry import slash_command
from integrations.mail.client import MailCLI, MailCLIError


def _compute_stats(db_path: str = None) -> str:
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

    lines.append("")
    lines.append("### 📧 账户分布")
    for r in acct_rows:
        lines.append(f"  {r['account_name']}: {r['cnt']} 封")

    return "\n".join(lines)


@skill(
    name="mail_stats",
    description="邮件处理统计：分类/重要性/状态/成功率分布",
    params={},
    guest_ok=False,
)
def mail_stats() -> str:
    return _compute_stats()


@slash_command('/mail_stats', category='邮件管理',
               description='邮件处理统计：分类/重要性/状态/成功率分布',
               show_in_dashboard=True)
def _cmd_mail_stats(agent, msg, args):
    return _compute_stats()
