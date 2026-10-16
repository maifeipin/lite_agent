"""
标准 SMTP 客户端实现 (integrations.smtp.client)。
纯基于 Python 标准库 smtplib 与 email.mime，零外部模块依赖，零 sys.path 污染。
"""

import os
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger("integrations.smtp")


class SMTPClient:
    """独立标准 SMTP 协议客户端。"""

    def __init__(self, host: str, port: int, username: str, password: str,
                 use_ssl: bool = True, use_tls: bool = False, timeout: float = 15.0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.use_tls = use_tls
        self.timeout = timeout

    def send(self, recipient: str, subject: str, text: str, html: Optional[str] = None,
             sender_conn_factory: Optional[Callable] = None) -> bool:
        """
        发送一封 MIME multipart/alternative 格式的邮件。
        :param sender_conn_factory: 可选的 SMTP 连接工厂，用于单元测试注入 Fake SMTP。
        """
        if not self.host or not self.username or not recipient:
            raise ValueError(f"SMTP 配置不全: host={self.host}, user={self.username}, to={recipient}")

        msg = MIMEMultipart("alternative")
        msg["From"] = self.username
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.attach(MIMEText(text, "plain", "utf-8"))
        if html:
            msg.attach(MIMEText(html, "html", "utf-8"))

        if sender_conn_factory:
            smtp = sender_conn_factory(self)
        else:
            if self.use_ssl or self.port == 465:
                smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                if self.use_tls or self.port == 587:
                    smtp.starttls()

            if self.username and self.password:
                smtp.login(self.username, self.password)

        try:
            smtp.send_message(msg)
            return True
        finally:
            try:
                smtp.quit()
            except Exception:
                # 退出阶段的异常不应导致重发或判定投递失败
                pass


def _extract_smtp_accounts(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    从配置中提取标准 SMTP 账户列表。
    支持新版 smtp 配置块、output_delivery.email 配置块、provider_order 及环境变量覆盖。
    """
    accounts: List[Dict[str, Any]] = []

    # 1. 检查顶级 smtp 配置
    smtp_cfg = config.get("smtp", {}) or {}
    if smtp_cfg.get("enabled", True) and smtp_cfg.get("host"):
        port = int(smtp_cfg.get("port", 465))
        # 587 默认 STARTTLS (use_ssl=False, use_tls=True)，465 默认 SSL
        default_ssl = False if port == 587 else True
        default_tls = True if port == 587 else False
        accounts.append({
            "provider": smtp_cfg.get("provider", "default_smtp"),
            "host": smtp_cfg.get("host", ""),
            "port": port,
            "username": smtp_cfg.get("username") or smtp_cfg.get("account", ""),
            "password": smtp_cfg.get("password", ""),
            "use_ssl": smtp_cfg.get("use_ssl", default_ssl),
            "use_tls": smtp_cfg.get("use_tls", default_tls),
        })

    # 2. 检查 output_delivery.email 中的直接字段配置
    out_cfg = config.get("output_delivery", {}) or {}
    email_cfg = out_cfg.get("email", {}) or {}
    if email_cfg.get("host") and (email_cfg.get("username") or email_cfg.get("account")):
        port = int(email_cfg.get("port", 465))
        default_ssl = False if port == 587 else True
        default_tls = True if port == 587 else False
        accounts.append({
            "provider": email_cfg.get("provider", "output_email"),
            "host": email_cfg.get("host", ""),
            "port": port,
            "username": email_cfg.get("username") or email_cfg.get("account", ""),
            "password": email_cfg.get("password", ""),
            "use_ssl": email_cfg.get("use_ssl", default_ssl),
            "use_tls": email_cfg.get("use_tls", default_tls),
        })

    # 3. 检查 accounts 列表配置（如有显式提供）
    for acc in (smtp_cfg.get("accounts") or email_cfg.get("accounts") or []):
        if isinstance(acc, dict) and acc.get("host") and (acc.get("account") or acc.get("username")):
            port = int(acc.get("port", 465))
            default_ssl = False if port == 587 else True
            default_tls = True if port == 587 else False
            accounts.append({
                "provider": acc.get("provider", "custom_account"),
                "host": acc.get("host", ""),
                "port": port,
                "username": acc.get("account") or acc.get("username", ""),
                "password": acc.get("password", ""),
                "use_ssl": acc.get("use_ssl", default_ssl),
                "use_tls": acc.get("use_tls", default_tls),
            })

    # 4. 环境变量优先覆盖或注入主账户
    env_host = os.environ.get("LITE_AGENT_SMTP_HOST")
    if env_host:
        env_port = int(os.environ.get("LITE_AGENT_SMTP_PORT", 465))
        env_user = os.environ.get("LITE_AGENT_SMTP_USER", "")
        env_pwd = os.environ.get("LITE_AGENT_SMTP_PASSWORD", "")
        env_ssl_raw = os.environ.get("LITE_AGENT_SMTP_USE_SSL")
        if env_ssl_raw is not None:
            use_ssl = env_ssl_raw.lower() in ("true", "1", "yes")
            use_tls = not use_ssl
        else:
            use_ssl = False if env_port == 587 else True
            use_tls = True if env_port == 587 else False

        accounts.insert(0, {
            "provider": "env_smtp",
            "host": env_host,
            "port": env_port,
            "username": env_user,
            "password": env_pwd,
            "use_ssl": use_ssl,
            "use_tls": use_tls,
        })

    # 5. 支持 provider_order 排序
    order = email_cfg.get("provider_order") or smtp_cfg.get("provider_order")
    if order and isinstance(order, list):
        def _order_key(acc):
            prov = acc.get("provider", "")
            return order.index(prov) if prov in order else len(order)
        accounts.sort(key=_order_key)

    return accounts


def send_email_delivery(
    text: str,
    config: Dict[str, Any],
    title: str = "",
    html_converter: Optional[Callable[[str], str]] = None,
    sender_factory: Optional[Callable] = None,
) -> str:
    """
    通用邮件外发入口。
    纯基于标准协议，解耦 Parser。
    成功返回 provider 标识，失败返回空字符串。
    """
    out_cfg = config.get("output_delivery", {}) or {}
    email_cfg = out_cfg.get("email", {}) or {}
    smtp_cfg = config.get("smtp", {}) or {}

    env_enabled = os.environ.get("LITE_AGENT_SMTP_ENABLED")
    if env_enabled is not None:
        enabled = env_enabled.lower() in ("true", "1", "yes")
    else:
        enabled = email_cfg.get("enabled", False) or smtp_cfg.get("enabled", False)

    if not enabled:
        return ""

    recipient = (
        os.environ.get("LITE_AGENT_SMTP_RECIPIENT")
        or email_cfg.get("recipient")
        or smtp_cfg.get("recipient")
        or ""
    ).strip()
    if not recipient:
        logger.warning("[SMTP] 邮件投递已启用但未配置收件人 recipient")
        return ""

    accounts = _extract_smtp_accounts(config)
    if not accounts:
        logger.warning("[SMTP] 未配置任何可用 SMTP 账户信息")
        return ""

    env_prefix = os.environ.get("LITE_AGENT_EMAIL_PREFIX")
    prefix = env_prefix or email_cfg.get("subject_prefix") or smtp_cfg.get("subject_prefix") or "[Lite Agent 完整回复]"
    subject = f"{prefix} {title}".strip()
    html_body = html_converter(text) if html_converter else None

    for acc in accounts:
        provider = acc.get("provider", "unknown")
        client = SMTPClient(
            host=acc["host"],
            port=acc["port"],
            username=acc["username"],
            password=acc["password"],
            use_ssl=acc.get("use_ssl", True),
            use_tls=acc.get("use_tls", False),
        )
        try:
            client.send(
                recipient=recipient,
                subject=subject,
                text=text,
                html=html_body,
                sender_conn_factory=sender_factory,
            )
            logger.info(f"[SMTP] 邮件通过 {provider} 成功外发")
            return provider
        except Exception as e:
            logger.warning(f"[SMTP] 通过 {provider} 发送邮件失败: {e}")

    return ""
