#!/usr/bin/env python3
"""邮件搜索技能 - /search [replytype] <keyword>

replytype: 0=截断摘要(默认), 1=分批(待实现), 2=上传HedgeDoc+链接
搜 email_bodies 正文库(Phase 2 已存),不连邮箱,毫秒级。
"""
import os, sqlite3
from datetime import datetime
from core.skill_engine import skill
from core.utils.hedgedoc import upload_to_hedgedoc

_SCRIPT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "mail-statement-parser"
)
_DB = os.path.join(_SCRIPT_DIR, "statements.db")


def _do_search(keyword, limit=20, account_name=None):
    """查 email_bodies JOIN email_summaries,返回完整 rows(含 plain_text 全文)"""
    if not os.path.exists(_DB):
        return []
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT es.subject, es.sender, es.email_date, es.category, es.importance, "
            "es.status, es.account_name, es.uid, eb.plain_text "
            "FROM email_bodies eb JOIN email_summaries es "
            "ON eb.account_name=es.account_name AND eb.uid=es.uid "
            "WHERE eb.plain_text LIKE ? OR es.subject LIKE ? OR es.sender LIKE ? "
        )
        params = [f"%{keyword}%"] * 3
        if account_name:
            sql += "AND es.account_name = ? "
            params.append(account_name)
        sql += "ORDER BY es.id DESC LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _format_summary(keyword, rows):
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


def _format_full_markdown(keyword, rows):
    """完整 Markdown(上传 HedgeDoc 用)"""
    lines = [
        f"# 邮件搜索: {keyword}",
        f"\n> 共 {len(rows)} 封 · {datetime.now().isoformat()}",
        "\n---",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(f"\n## {i}. {r['subject']}")
        lines.append(f"- 发件人: {r['sender']}")
        lines.append(f"- 日期: {r['email_date']}")
        lines.append(f"- 分类: {r['category']} / 重要性: {r['importance']} / 状态: {r['status']}")
        lines.append(f"- 账户: {r['account_name']} (uid: {r['uid']})")
        lines.append(f"\n### 正文\n\n{r['plain_text'] or '(无正文)'}")
        lines.append("\n---")
    return "\n".join(lines)


def _get_hedgedoc_config():
    """复用 ops_web_clipper 的 HedgeDoc 配置读取"""
    from skills.ops_web_clipper import _get_hedgedoc_config as _ghc
    return _ghc()


@skill(
    name="mail_search",
    description="全文搜索邮件正文/主题/发件人(replytype:0=摘要,1=分批,2=HedgeDoc完整文档)",
    params={
        "keyword": {"type": "string", "description": "搜索关键词"},
        "replytype": {"type": "integer", "description": "0=摘要(默认),1=分批,2=HedgeDoc完整文档", "default": 0},
        "limit": {"type": "integer", "description": "返回上限(默认20)", "default": 20},
        "account_name": {"type": "string", "description": "账户(可选)", "default": None},
    },
    guest_ok=False,
)
def mail_search(keyword: str, replytype: int = 0, limit: int = 20, account_name: str = None) -> str:
    rows = _do_search(keyword, limit, account_name)
    if not rows:
        return f"🔍 未找到包含 '{keyword}' 的邮件(已搜 email_bodies 正文库)。"

    if replytype == 2:
        # 上传完整 Markdown 到 HedgeDoc,返回链接 + 前 3 封摘要
        md = _format_full_markdown(keyword, rows)
        hc = _get_hedgedoc_config()
        url = upload_to_hedgedoc(md, hc)
        if url:
            summary = _format_summary(keyword, rows[:3])
            return (
                f"📊 搜索 '{keyword}' 找到 {len(rows)} 封,完整内容已上传 HedgeDoc:\n"
                f"🔗 {url}\n\n"
                f"{summary}\n\n"
                f"_(完整正文见上方链接)_"
            )
        return f"❌ HedgeDoc 上传失败,改用摘要:\n\n{_format_summary(keyword, rows)}"

    elif replytype == 1:
        # 分批待实现,暂用摘要兜底
        return f"_(分批待实现,先用摘要)_\n\n{_format_summary(keyword, rows)}"

    return _format_summary(keyword, rows)
