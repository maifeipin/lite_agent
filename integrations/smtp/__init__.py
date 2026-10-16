"""
标准 SMTP 协议客户端集成包 (integrations.smtp)。
纯基于 Python 标准库，解耦外部 Parser，支持 SSL/TLS 与多账号故障切换。
"""

from integrations.smtp.client import SMTPClient, send_email_delivery

__all__ = ["SMTPClient", "send_email_delivery"]
