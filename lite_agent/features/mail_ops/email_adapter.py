"""
Mail Ops 邮件 HTML 渲染 HTTP 业务 Adapter (features.mail_ops.email_adapter).
从公共宿主解耦邮件 HTML 原生渲染。
"""

import os
import sqlite3
import html
from urllib.parse import parse_qs
from typing import Any


def handle_email_html(http_handler: Any, parsed_url: Any) -> None:
    """直接返回邮件原始 HTML，供浏览器原生渲染预览。
    GET /api/v1/email/html?account=<account>&uid=<uid>
    """
    qs = parse_qs(parsed_url.query)
    account = (qs.get('account', [None])[0] or '').strip()
    uid = (qs.get('uid', [None])[0] or '').strip()
    if not account or not uid:
        http_handler.send_error(400, "Bad Request: Missing account or uid")
        return

    from lite_agent.integrations.mail.paths import get_statement_db_path
    db_path = get_statement_db_path()
    if not os.path.exists(db_path):
        http_handler.send_error(500, "Database not found")
        return

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT es.subject, es.sender, es.email_date, eb.raw_html, eb.plain_text "
            "FROM email_bodies eb "
            "JOIN email_summaries es ON eb.account_name=es.account_name AND eb.uid=es.uid "
            "WHERE eb.account_name=? AND eb.uid=?", (account, uid)
        )
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        http_handler.send_error(500, f"Database error: {e}")
        return

    if not row:
        http_handler.send_error(404, "Email not found")
        return

    subject, sender, email_date, raw_html, plain_text = row
    if raw_html:
        html_content = raw_html
    elif plain_text:
        html_content = f"<html><head><meta charset='utf-8'><title>{html.escape(subject or '')}</title></head><body><pre>{html.escape(plain_text)}</pre></body></html>"
    else:
        http_handler.send_error(404, "Email body is empty")
        return

    body_bytes = html_content.encode('utf-8')
    http_handler.send_response(200)
    if hasattr(http_handler, "_send_cors_headers"):
        http_handler._send_cors_headers()
    http_handler.send_header('Content-Type', 'text/html; charset=utf-8')
    http_handler.send_header('Content-Length', str(len(body_bytes)))
    http_handler.end_headers()
    http_handler.wfile.write(body_bytes)
