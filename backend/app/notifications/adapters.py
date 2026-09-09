"""Email notification adapters and protocols (Spec 3.4, 6.3, 13.4)."""

import asyncio
import logging
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage as PyEmailMessage
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
    """Production SMTP email adapter using smtplib inside asyncio.to_thread (Spec 6.3, 13.4).

    Sequence: connect, EHLO, STARTTLS, EHLO, login, send_message, quit.
    Fixed port: 587. Socket timeout: 10 seconds.
    Safe error categories: timeout, tls, authentication, recipient_rejected, transport.
    No credentials, recipient emails, or message bodies are logged in production.
    """

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings

    def _send_sync(self, message: EmailMessage) -> str:
        if not getattr(self.settings, "EMAIL_ENABLED", False):
            raise EmailDeliveryError("transport")

        host = getattr(self.settings, "SMTP_HOST", "")
        port = getattr(self.settings, "SMTP_PORT", 587)
        username = getattr(self.settings, "SMTP_USERNAME", "")
        password = getattr(self.settings, "SMTP_PASSWORD", "")
        email_from = getattr(self.settings, "EMAIL_FROM", "") or "noreply@commonsbook.invalid"
        app_env = getattr(self.settings, "APP_ENV", "local")

        if app_env in ("local", "test"):
            logger.info(
                "Connecting to SMTP server at %s:%s for message %s", host, port, message.message_id
            )
        else:
            logger.info("Sending notification message %s", message.message_id)

        mime_msg = PyEmailMessage()
        mime_msg["Message-ID"] = message.message_id
        mime_msg["To"] = message.to
        mime_msg["From"] = email_from
        mime_msg["Subject"] = message.subject
        mime_msg.set_content(message.text_body)

        smtp = None
        try:
            smtp = smtplib.SMTP(host, port, timeout=10.0)
            smtp.ehlo()
            context = ssl.create_default_context()
            smtp.starttls(context=context)
            smtp.ehlo()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(mime_msg)
            try:
                smtp.quit()
            except Exception:
                pass
        except (TimeoutError, socket.timeout):
            logger.warning("SMTP delivery timed out for message %s", message.message_id)
            raise EmailDeliveryError("timeout") from None
        except ssl.SSLError:
            logger.warning("SMTP TLS negotiation failed for message %s", message.message_id)
            raise EmailDeliveryError("tls") from None
        except smtplib.SMTPAuthenticationError:
            logger.warning("SMTP authentication failed for message %s", message.message_id)
            raise EmailDeliveryError("authentication") from None
        except smtplib.SMTPRecipientsRefused:
            logger.warning("SMTP recipient refused for message %s", message.message_id)
            raise EmailDeliveryError("recipient_rejected") from None
        except (smtplib.SMTPException, OSError):
            logger.warning(
                "SMTP transport error for message %s: category=transport", message.message_id
            )
            raise EmailDeliveryError("transport") from None

        return message.message_id

    async def send(self, message: EmailMessage) -> str:
        return await asyncio.to_thread(self._send_sync, message)
