# Mail integrations package
from integrations.mail.client import MailCLI
from integrations.mail.quality_todo import consume_quality_events

__all__ = ["MailCLI", "consume_quality_events"]
