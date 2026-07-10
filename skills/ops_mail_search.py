#!/usr/bin/env python3
"""邮件搜索技能 - AI 工具 + /search 直通指令

搜 email_bodies 正文 + email_summaries 元数据(db 全文 LIKE),不实时连邮箱。
Phase 2 已持久化 223 封正文,直接查库即可(快、支持中文、不超时)。
Phase 4 装 Meilisearch 后可升级为索引搜索(更快、分词)。
"""

import os, sqlite3
from core.skill_engine import skill

_SCRIPT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "mail-statement-parser"
)
_DB = os.path.join(_SCRIPT_DIR, "statements.db")


def _search_email_bodies(keyword: str, limit: int = 20, account_name: str = None) -> str:
    if not os.path.exists(_DB):
        return "❌ 邮件数据库不存在,请先运行 mail_fetch_summaries 初始化。"
    if not keyword.strip():
        return "❌ 请提供搜索关键词。"

    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    try:
        # JOIN email_bodies(正文) + email_summaries(元数据),搜 正文/主题/发件人
        sql = (
            "SELECT es.subject, es.sender, es.email_date, es.category, es.importance, "
            "es.status, substr(eb.plain_text,1,150) AS snippet "
            "FROM email_bodies eb "
            "JOIN email_summaries es ON eb.account_name=es.account_name AND eb.uid=es.uid "
            "WHERE eb.plain_text LIKE ? OR es.subject LIKE ? OR es.sender LIKE ? "
        )
        params = [f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"]
        if account_name:
            sql += "AND es.account_name = ? "
            params.append(account_name)
        sql += "ORDER BY es.id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    if not rows:
        return f"🔍 未找到包含 '{keyword}' 的邮件(已搜 {223} 封正文库)。"

    lines = [f"🔍 搜索 '{keyword}' 找到 {len(rows)} 封:"]
    for r in rows:
        snippet = (r["snippet"] or "").replace("\n", " ")[:80]
        lines.append(
            f"\n[{r['category']}/{r['importance']}] {r['subject'][:50]}\n"
            f"  发件人: {r['sender']}\n"
            f"  摘录: {snippet}"
        )
    return "\n".join(lines)


@skill(
    name="mail_search",
    description="全文搜索邮件正文/主题/发件人(基于 email_bodies 库,不连邮箱)",
    params={
        "keyword": {"type": "string", "description": "搜索关键词"},
        "limit": {"type": "integer", "description": "返回上限 (默认20)", "default": 20},
        "account_name": {"type": "string", "description": "邮箱账户名 (可选)", "default": None},
    },
    guest_ok=False,
)
def mail_search(keyword: str, limit: int = 20, account_name: str = None) -> str:
    return _search_email_bodies(keyword, limit, account_name)
