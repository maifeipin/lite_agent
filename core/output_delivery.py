"""Reliable delivery of complete model output.

The model/runtime produces text; this module only decides where the complete
text is stored when an IM channel cannot carry it. External destinations are
configuration-driven and independent from model providers.
"""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import importlib
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from core.constants import PROJECT_ROOT


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
    accounts = list(mail_client.load_accounts() or [])
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
                    message = MIMEMultipart()
                    message["From"] = account["account"]
                    message["To"] = recipient
                    message["Subject"] = subject
                    message.attach(MIMEText(text, "plain", "utf-8"))
                    smtp.send_message(message)
                finally:
                    smtp.quit()
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
                           summarize: Optional[Callable[[str, int], str]] = None) -> str:
    """Return channel-safe text while preserving the complete response elsewhere."""
    policy = resolve_output_policy(config, overrides)
    cfg = config.get("output_delivery", {}) or {}
    limits = {**DEFAULT_CHANNEL_LIMITS, **(cfg.get("channel_limits", {}) or {})}
    max_chars = policy["max_chars"] or int(limits.get(channel.lower(), 2500))

    # API callers retain complete inline output unless explicitly overridden.
    # Explicit external destinations also run for short text.
    if policy["full_delivery"] == "inline" or (
        channel.lower() == "api" and not policy["explicit"]
    ):
        return text
    needs_external = len(text) > max_chars or policy["explicit"]
    if not needs_external:
        return text

    destination, location = _deliver_complete(text, channel, config, policy, title)
    summary = _summary_or_preview(text, policy, summarize)
    if destination == "hedgedoc":
        suffix = f"\n\n完整回复已保存为 HedgeDoc 公开链接：\n{location}"
    elif destination == "email":
        suffix = f"\n\n完整回复已发送到配置邮箱（发送通道：{location}）。"
    elif destination == "sqlite":
        suffix = f"\n\n外部发送均不可用，完整回复已保存到本地 SQLite（归档 ID：{location}）。"
    else:
        # Session history still owns the original complete reply. This is the
        # final transport-only fallback if local archive creation also fails.
        suffix = "\n\n⚠️ 完整回复外部转存失败；原文仍保留在会话历史中。"

    available = max(0, max_chars - len(suffix))
    return summary[:available].rstrip() + suffix
