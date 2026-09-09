"""Email notification adapters and protocols (Spec 3.4, 6.3, 13.4)."""

import logging
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger("notifications.adapters")


@dataclass(frozen=True)
class EmailMessage:
    message_id: str
    to: str
    subject: str
    text_body: str


class EmailAdapter(Protocol):
    async def send(self, message: EmailMessage) -> str:
        """Send message and return provider message ID."""
        ...


class EmailDeliveryError(Exception):
    """Raised on notification delivery failure with safe category only (Spec 13.4).

    Allowed categories: timeout, tls, authentication, recipient_rejected, transport.
    No raw exception text reaches a response or log.
    """

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class ConsoleEmailAdapter:
    """Console email adapter for local development and test environments."""

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings

    async def send(self, message: EmailMessage) -> str:
        app_env = getattr(self.settings, "APP_ENV", "local") if self.settings else "local"
        if app_env in ("local", "test"):
            logger.info(
                "ConsoleEmailAdapter sending email: ID=%s To=%s Subject=%s Body=%s",
                message.message_id,
                message.to,
                message.subject,
                message.text_body,
            )
        else:
            logger.info("ConsoleEmailAdapter sending email: ID=%s", message.message_id)
        return message.message_id


class SMTPEmailAdapter:
    """Production SMTP email adapter (Spec 6.3, 13.4)."""

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings

    async def send(self, message: EmailMessage) -> str:
        raise NotImplementedError("SMTPEmailAdapter send implementation arrives in Phase 3")
