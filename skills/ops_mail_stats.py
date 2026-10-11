#!/usr/bin/env python3
"""邮件统计技能 - AI 工具 + /mailstats 直通指令
统计 email_summaries 表的分类占比 / 重要性分布 / 处理成功率。
"""

import sys, os, sqlite3
from core.skill_engine import skill
from core.command_registry import slash_command

from integrations.mail.paths import connect_readonly_db, get_statement_db_path


def _compute_stats(db_path: str = None) -> str:
    conn = connect_readonly_db(db_path)
    if conn is None:
        return "❌ 邮件数据库不存在，请先运行 mail_fetch_summaries 初始化。"

    try:
        cur = conn.cursor()

        # 总量
        total = cur.execute("SELECT count(*) FROM email_summaries").fetchone()[0]
        if total == 0:
            return "📭 暂无邮件记录。"

        # 分类分布
        cat_rows = cur.execute(
            "SELECT category, count(*) as cnt FROM email_summaries GROUP BY category ORDER BY cnt DESC"
        ).fetchall()

        # 重要性分布
        imp_rows = cur.execute(
            "SELECT importance, count(*) as cnt FROM email_summaries GROUP BY importance ORDER BY cnt DESC"
        ).fetchall()

        # 状态分布
        status_rows = cur.execute(
            "SELECT status, count(*) as cnt FROM email_summaries GROUP BY status"
        ).fetchall()
        status_map = {r["status"]: r["cnt"] for r in status_rows}
        processed = status_map.get("processed", 0)
        failed = status_map.get("failed", 0)
        pending = status_map.get("pending", 0)
        noise = status_map.get("noise", 0)

        # 账户分布
        acct_rows = cur.execute(
            "SELECT account_name, count(*) as cnt FROM email_summaries GROUP BY account_name ORDER BY cnt DESC"
        ).fetchall()

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
            success_rate = (processed + noise) / total * 100
            lines.append(f"  📈 成功率: {success_rate:.1f}%")

        lines.append("")
        lines.append("### 📧 账户分布")
        for r in acct_rows:
            lines.append(f"  {r['account_name']}: {r['cnt']} 封")

        return "\n".join(lines)
    finally:
        conn.close()


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
