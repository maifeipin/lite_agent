"""Reliable delivery of complete model output.

The model/runtime produces text; this module only decides where the complete
text is stored when an IM channel cannot carry it. External destinations are
configuration-driven and independent from model providers.
"""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import html as _html
import importlib
import os
import re
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from core.constants import PROJECT_ROOT


def _markdown_to_html(text: str) -> str:
    """内置零依赖 Markdown → HTML 子集转换（标题/表格/无序列表/粗体/段落）。

    仅用于邮件 multipart/alternative 的 HTML 副本；原文纯文本始终一并附上，
    未覆盖的 Markdown 语法按原样文本输出，不影响金额等数据内容。
    """
    def inline(s: str) -> str:
        s = _html.escape(s)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)

    def is_table_sep(line: str) -> bool:
        return bool(re.fullmatch(r"\|?[\s:\-|]+\|?", line.strip())) and "-" in line

    def split_row(row: str) -> list:
        # 按未转义管道拆列；报告生成方会把字段内的 | 转义为 \|
        parts = [p.strip().replace("\\|", "|")
                 for p in re.split(r"(?<!\\)\|", row.strip())]
        if parts and parts[0] == "":
            parts = parts[1:]
        if parts and parts[-1] == "":
            parts = parts[:-1]
        return parts

    def render_table(rows: list) -> str:
        cells = [split_row(row) for row in rows]
        width = len(cells[0]) if cells else 0
        if width == 0 or any(len(r) != width for r in cells[2:]):
            # 列数与表头对不齐时降级为原文，避免金额等字段错列
            return "<pre>" + _html.escape("\n".join(rows)) + "</pre>"
        out = ["<table><thead><tr>"]
        out += [f"<th>{inline(c)}</th>" for c in cells[0]]
        out.append("</tr></thead><tbody>")
        for row in cells[2:]:
            out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>")
        out.append("</tbody></table>")
        return "".join(out)

    body, para, list_items, i = [], [], [], 0
    lines = text.splitlines()

    def flush_para():
        if para:
            body.append("<p>" + "<br/>".join(inline(x) for x in para) + "</p>")
            para.clear()

    def flush_list():
        if list_items:
            body.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in list_items) + "</ul>")
            list_items.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("|") and i + 1 < len(lines) and is_table_sep(lines[i + 1]):
            flush_para()
            flush_list()
            rows = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            body.append(render_table(rows))
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            flush_para()
            flush_list()
            level = len(heading.group(1))
            body.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        elif stripped.startswith("- "):
            flush_para()
            list_items.append(stripped[2:])
        elif not stripped:
            flush_para()
            flush_list()
        else:
            flush_list()
            para.append(line)
        i += 1
    flush_para()
    flush_list()

    return (
        '<html><head><meta charset="utf-8"><style>'
        "body{font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
        "line-height:1.6;color:#222;font-size:14px}"
        "table{border-collapse:collapse;margin:8px 0}"
        "th,td{border:1px solid #ccc;padding:4px 10px;text-align:left}"
        "th{background:#f5f5f5}"
        "</style></head><body>" + "".join(body) + "</body></html>"
    )


DEFAULT_CHANNEL_LIMITS = {
    "telegram": 4000,
    "feishu": 4000,
    "dingtalk": 2500,
    "wecom": 2048,
    # WeChat transport limits bytes and context sends. This conservative
    # character limit leaves room for the title and multibyte Chinese text.
    "wechat": 2000,
}
VALID_FULL_DELIVERY = {"auto", "email", "hedgedoc", "sqlite", "inline"}
_MAIL_CONFIG_LOCK = threading.Lock()


def resolve_output_policy(config: dict, overrides: dict = None) -> dict:
    cfg = config.get("output_delivery", {}) or {}
    long_cfg = cfg.get("long_output", {}) or {}
    overrides = overrides or {}
    requested = overrides.get("full_delivery") or overrides.get("output_mode")
    requested = requested if requested in VALID_FULL_DELIVERY else ""
    full_delivery = requested or long_cfg.get("full_delivery", "auto")
    # Compatibility for an unreleased early TaskSpec/output policy.
    legacy_overflow = overrides.get("overflow")
    if not requested and legacy_overflow in {"hedgedoc", "inline"}:
        full_delivery = legacy_overflow
    if full_delivery not in VALID_FULL_DELIVERY:
        full_delivery = "auto"
    return {
        "full_delivery": full_delivery,
        "reply_mode": overrides.get(
            "reply_mode", long_cfg.get("reply_mode", "summary")
        ),
        "fallback_order": list(
            overrides.get("fallback_order")
            or long_cfg.get("fallback_order")
            or ["hedgedoc", "email", "sqlite"]
        ),
        "max_chars": int(overrides.get("max_chars", 0) or 0),
        "summary_chars": int(long_cfg.get("summary_chars", 1200) or 1200),
        "explicit": bool(requested),
        # 显式 hedgedoc 时可置 False: 上传失败不转投邮件, 归档并如实报失败
        "allow_email_fallback": bool(overrides.get("allow_email_fallback", True)),
    }


def _archive_db_path(config: dict) -> str:
    sqlite_cfg = (config.get("output_delivery", {}) or {}).get("sqlite", {}) or {}
    path = sqlite_cfg.get("path", "data/output_archive.db")
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def archive_output(text: str, channel: str, config: dict, title: str = "") -> str:
    """Persist complete text locally and return a non-secret archive id."""
    db_path = _archive_db_path(config)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    archive_id = uuid.uuid4().hex[:16]
    with sqlite3.connect(db_path, timeout=5.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS output_archive (
                id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO output_archive(id, channel, title, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (archive_id, channel, title, text, datetime.now(timezone.utc).isoformat()),
        )
    return archive_id


def get_archived_output(archive_id: str, config: dict) -> Optional[dict]:
    db_path = _archive_db_path(config)
    if not os.path.exists(db_path):
        return None
    with sqlite3.connect(db_path, timeout=5.0) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, channel, title, content, created_at "
            "FROM output_archive WHERE id=?", (archive_id,)
        ).fetchone()
    return dict(row) if row else None


def _send_email(text: str, config: dict, title: str = "") -> str:
    """Reuse the deployed ops_mail account stack; return provider on success."""
    output_cfg = config.get("output_delivery", {}) or {}
    email_cfg = output_cfg.get("email", {}) or {}
    if not email_cfg.get("enabled"):
        return ""
    recipient = str(email_cfg.get("recipient") or "").strip()
    if not recipient:
        return ""
    script_dir = email_cfg.get("script_dir") or (
        config.get("billing", {}) or {}
    ).get("script_dir", "/home/liteagent/mail-statement-parser")
    if not os.path.isfile(os.path.join(script_dir, "mail_client.py")):
        return ""

    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    mail_client = importlib.import_module("mail_client")
    mail_connect = importlib.import_module("mail_connect")
    # The reused ops_mail module defines config candidates relative to its
    # process cwd. Lite Agent runs from a different project directory, so make
    # those candidates absolute without changing the process-wide cwd.
    with _MAIL_CONFIG_LOCK:
        original_candidates = getattr(mail_client, "CONFIG_CANDIDATES", None)
        if isinstance(original_candidates, list):
            mail_client.CONFIG_CANDIDATES = [
                value if os.path.isabs(value) else os.path.join(script_dir, value)
                for value in original_candidates
            ]
        try:
            accounts = list(mail_client.load_accounts() or [])
        except SystemExit:
            print("  ⚠️ [OutputDelivery] 邮件账号配置加载失败")
            return ""
        finally:
            if isinstance(original_candidates, list):
                mail_client.CONFIG_CANDIDATES = original_candidates
    provider_order = list(
        email_cfg.get("provider_order") or ["qq", "163", "outlook", "gmail"]
    )
    rank = {name: index for index, name in enumerate(provider_order)}
    accounts.sort(key=lambda item: rank.get(item.get("provider"), len(rank)))
    subject = f"{email_cfg.get('subject_prefix', '[Lite Agent 完整回复]')} {title}".strip()

    last_error = None
    for account in accounts:
        provider = str(account.get("provider") or "unknown")
        if provider not in rank:
            continue
        try:
            if mail_connect.is_graph_api(account):
                mail_connect.send_email_graph(account, recipient, subject, text)
            else:
                smtp = mail_connect.connect_smtp(account)
                try:
                    message = MIMEMultipart("alternative")
                    message["From"] = account["account"]
                    message["To"] = recipient
                    message["Subject"] = subject
                    message.attach(MIMEText(text, "plain", "utf-8"))
                    message.attach(MIMEText(_markdown_to_html(text), "html", "utf-8"))
                    smtp.send_message(message)
                finally:
                    try:
                        smtp.quit()
                    except Exception:
                        # A teardown failure does not invalidate an accepted
                        # message or justify sending it again via another account.
                        pass
            return provider
        except Exception as exc:
            last_error = exc
            print(f"  ⚠️ [OutputDelivery] 邮件通道 {provider} 失败: {type(exc).__name__}")
    if last_error:
        print("  ⚠️ [OutputDelivery] 所有已配置邮件通道均发送失败")
    return ""


def _delivery_order(policy: dict) -> list[str]:
    selected = policy["full_delivery"]
    if selected == "inline":
        return []
    configured = [
        item for item in policy["fallback_order"]
        if item in {"hedgedoc", "email", "sqlite"}
    ]
    if selected == "auto":
        return configured
    if selected == "email":
        # Choosing a private destination must not silently escalate to a public
        # HedgeDoc note when SMTP fails.
        return ["email", "sqlite"]
    if selected == "sqlite":
        return ["sqlite"]
    if selected == "hedgedoc" and not policy.get("allow_email_fallback", True):
        # 只传 HedgeDoc: 上传失败直接归档并如实报失败, 不转投邮件假成功
        return ["hedgedoc", "sqlite"]
    return [selected] + [item for item in configured if item != selected]


def _deliver_complete(text: str, channel: str, config: dict,
                      policy: dict, title: str) -> tuple[str, str]:
    for destination in _delivery_order(policy):
        try:
            if destination == "hedgedoc":
                hedgedoc = config.get("hedgedoc", {}) or {}
                if hedgedoc.get("enabled"):
                    from core.utils.hedgedoc import upload_to_hedgedoc
                    location = upload_to_hedgedoc(text, hedgedoc)
                    if location:
                        return "hedgedoc", location
            elif destination == "email":
                provider = _send_email(text, config, title=title)
                if provider:
                    return "email", provider
            elif destination == "sqlite":
                return "sqlite", archive_output(text, channel, config, title=title)
        except (Exception, SystemExit) as exc:
            print(
                f"  ⚠️ [OutputDelivery] {destination} 失败，继续回退: "
                f"{type(exc).__name__}"
            )
    return "", ""


def _summary_or_preview(text: str, policy: dict,
                        summarize: Optional[Callable[[str, int], str]]) -> str:
    if policy["reply_mode"] == "summary" and summarize:
        try:
            summary = str(summarize(text, policy["summary_chars"]) or "").strip()
            if summary:
                return summary
        except Exception as exc:
            print(f"  ⚠️ [OutputDelivery] 摘要生成失败，使用回复预览: {type(exc).__name__}")
    return text[:policy["summary_chars"]].rstrip()


def prepare_channel_output(text: str, channel: str, config: dict,
                           overrides: dict = None, title: str = "",
                           summarize: Optional[Callable[[str, int], str]] = None,
                           task_result=None) -> str:
    """Return channel-safe text while preserving the complete response elsewhere."""
    policy = resolve_output_policy(config, overrides)
    cfg = config.get("output_delivery", {}) or {}
    limits = {**DEFAULT_CHANNEL_LIMITS, **(cfg.get("channel_limits", {}) or {})}
    max_chars = policy["max_chars"] or int(limits.get(channel.lower(), 2500))
    status_prefix = (task_result.status_text() + '\n\n') if task_result else ''

    # API callers retain complete inline output unless explicitly overridden.
    # Explicit external destinations also run for short text.
    if policy["full_delivery"] == "inline" or (
        channel.lower() == "api" and not policy["explicit"]
    ):
        return status_prefix + text
    needs_external = len(text) > max_chars or policy["explicit"]
    if not needs_external:
        return status_prefix + text

    previous = task_result.delivery if task_result else {}
    if previous.get('destination') == 'inline':
        # 用户要求不外发且工作流已完成本地归档: 频道层绝不重新外发,
        # 仅呈现摘要并指向已有归档 ID
        summary = _summary_or_preview(text, policy, None)
        note = f"\n\n完整内容已保存本地归档（归档 ID：{previous.get('archive_id', '')}）。"
        available = max(0, max_chars - len(note) - len(status_prefix))
        return status_prefix + summary[:available].rstrip() + note
    if previous.get('destination'):
        # 复用上游已完成的投递回执与归档 ID(submitted/archived 均算),
        # 频道层不得据此重新上传或重新发送
        destination, location = previous['destination'], previous.get('location', '')
    else:
        destination, location = _deliver_complete(status_prefix + text, channel, config, policy, title)
        if task_result:
            task_result.delivery = {'purpose': 'execution_report', 'status': 'submitted' if destination else 'failed',
                                    'destination': destination, 'location': location}
    # Structured execution reports already contain a bounded analysis near
    # the beginning; do not pay for another model pass over full detail tables.
    summary = _summary_or_preview(text, policy, None if task_result else summarize)
    if destination == "hedgedoc":
        suffix = f"\n\n完整回复已保存到 HedgeDoc（仅登录账号可见）：\n{location}"
    elif destination == "email":
        purpose = '账单报告' if previous.get('purpose') == 'billing_report' else '执行报告' if task_result else '完整回复'
        suffix = f"\n\n{purpose}已提交邮件通道（{location}），收件人使用项目配置。"
    elif destination == "sqlite":
        if policy["full_delivery"] == "sqlite":
            suffix = (
                f"\n\n已按任务设置将完整回复保存到本地 SQLite"
                f"（归档 ID：{location}）。"
            )
        else:
            suffix = (
                f"\n\n外部发送均不可用，完整回复已回退保存到本地 "
                f"SQLite（归档 ID：{location}）。"
            )
    else:
        # Session history still owns the original complete reply. This is the
        # final transport-only fallback if local archive creation also fails.
        suffix = "\n\n⚠️ 完整回复外部转存失败；原文仍保留在会话历史中。"

    available = max(0, max_chars - len(suffix) - len(status_prefix))
    return status_prefix + summary[:available].rstrip() + suffix
