"""Unit tests for SMTPEmailAdapter (Spec 6.3, 8.3, 9.2, 13.4).

Verifies local fake SMTP transport, STARTTLS/auth/port/timeout wiring,
safe error category mapping, secret redaction, and production log scrubbing.
"""

import logging
import smtplib
import socket
import ssl
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.notifications.adapters import EmailDeliveryError, EmailMessage, SMTPEmailAdapter


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        APP_ENV="local",
        EMAIL_ENABLED=True,
        EMAIL_ADAPTER="smtp",
        SMTP_HOST="smtp.testserver.invalid",
        SMTP_PORT=587,
        SMTP_USERNAME="test_user",
        SMTP_PASSWORD="supersecret_password_12345",
        EMAIL_FROM="notifications@commonsbook.invalid",
    )


@pytest.mark.asyncio
async def test_smtp_adapter_send_success_and_wiring(test_settings: Settings) -> None:
    """Verify SMTP connect, EHLO, STARTTLS, EHLO, login, send_message, quit sequence."""
    adapter = SMTPEmailAdapter(test_settings)
    message = EmailMessage(
        message_id="<test-event-uuid.test-user-uuid@commonsbook.invalid>",
        to="member@example.com",
        subject="Booking Confirmed: Room 101",
        text_body="Your booking is confirmed.",
    )

    mock_smtp_instance = MagicMock()
    with patch("smtplib.SMTP", return_value=mock_smtp_instance) as mock_smtp_cls:
        returned_id = await adapter.send(message)

        assert returned_id == message.message_id
        mock_smtp_cls.assert_called_once_with("smtp.testserver.invalid", 587, timeout=10.0)

        # Check call sequence: ehlo -> starttls -> ehlo -> login -> send_message -> quit
        assert mock_smtp_instance.ehlo.call_count == 2
        mock_smtp_instance.starttls.assert_called_once()
        mock_smtp_instance.login.assert_called_once_with("test_user", "supersecret_password_12345")
        mock_smtp_instance.send_message.assert_called_once()
        mock_smtp_instance.quit.assert_called_once()

        # Inspect the delivered MIME message
        sent_mime = mock_smtp_instance.send_message.call_args[0][0]
        assert sent_mime["Message-ID"] == message.message_id
        assert sent_mime["To"] == "member@example.com"
        assert sent_mime["From"] == "notifications@commonsbook.invalid"
        assert sent_mime["Subject"] == "Booking Confirmed: Room 101"
        assert sent_mime.get_content().strip() == "Your booking is confirmed."


@pytest.mark.asyncio
async def test_smtp_adapter_timeout_propagation(test_settings: Settings) -> None:
    """Timeout during connection or send maps to EmailDeliveryError('timeout')."""
    adapter = SMTPEmailAdapter(test_settings)
    message = EmailMessage(
        message_id="<timeout-event@commonsbook.invalid>",
        to="member@example.com",
        subject="Timeout test",
        text_body="Body",
    )

    with patch("smtplib.SMTP", side_effect=socket.timeout("Socket connection timed out")):
        with pytest.raises(EmailDeliveryError) as exc_info:
            await adapter.send(message)
        assert exc_info.value.category == "timeout"


@pytest.mark.asyncio
async def test_smtp_adapter_tls_error_propagation(test_settings: Settings) -> None:
    """SSL/TLS handshake failure maps to EmailDeliveryError('tls')."""
    adapter = SMTPEmailAdapter(test_settings)
    message = EmailMessage(
        message_id="<tls-event@commonsbook.invalid>",
        to="member@example.com",
        subject="TLS test",
        text_body="Body",
    )

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.starttls.side_effect = ssl.SSLError("Certificate validation failed")
    with patch("smtplib.SMTP", return_value=mock_smtp_instance):
        with pytest.raises(EmailDeliveryError) as exc_info:
            await adapter.send(message)
        assert exc_info.value.category == "tls"


@pytest.mark.asyncio
async def test_smtp_adapter_auth_error_propagation(test_settings: Settings) -> None:
    """SMTP authentication failure maps to EmailDeliveryError('authentication')."""
    adapter = SMTPEmailAdapter(test_settings)
    message = EmailMessage(
        message_id="<auth-event@commonsbook.invalid>",
        to="member@example.com",
        subject="Auth test",
        text_body="Body",
    )

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.login.side_effect = smtplib.SMTPAuthenticationError(
        535, b"5.7.8 Authentication credentials invalid"
    )
    with patch("smtplib.SMTP", return_value=mock_smtp_instance):
        with pytest.raises(EmailDeliveryError) as exc_info:
            await adapter.send(message)
        assert exc_info.value.category == "authentication"


@pytest.mark.asyncio
async def test_smtp_adapter_recipient_rejected_propagation(test_settings: Settings) -> None:
    """Refused recipient address maps to EmailDeliveryError('recipient_rejected')."""
    adapter = SMTPEmailAdapter(test_settings)
    message = EmailMessage(
        message_id="<recipient-refused@commonsbook.invalid>",
        to="invalid-user@example.com",
        subject="Recipient test",
        text_body="Body",
    )

    mock_smtp_instance = MagicMock()
    mock_smtp_instance.send_message.side_effect = smtplib.SMTPRecipientsRefused(
        {"invalid-user@example.com": (550, b"User mailbox not found")}
    )
    with patch("smtplib.SMTP", return_value=mock_smtp_instance):
        with pytest.raises(EmailDeliveryError) as exc_info:
            await adapter.send(message)
        assert exc_info.value.category == "recipient_rejected"


@pytest.mark.asyncio
async def test_smtp_adapter_transport_error_propagation(test_settings: Settings) -> None:
    """Generic network or transport failure maps to EmailDeliveryError('transport')."""
    adapter = SMTPEmailAdapter(test_settings)
    message = EmailMessage(
        message_id="<transport-event@commonsbook.invalid>",
        to="member@example.com",
        subject="Transport test",
        text_body="Body",
    )

    with patch("smtplib.SMTP", side_effect=ConnectionResetError("Connection reset by peer")):
        with pytest.raises(EmailDeliveryError) as exc_info:
            await adapter.send(message)
        assert exc_info.value.category == "transport"


@pytest.mark.asyncio
async def test_smtp_adapter_honors_email_enabled_false() -> None:
    """EMAIL_ENABLED=false must never initiate an SMTP network connection."""
    disabled_settings = Settings(
        APP_ENV="local",
        EMAIL_ENABLED=False,
        EMAIL_ADAPTER="smtp",
        SMTP_HOST="smtp.testserver.invalid",
        SMTP_PORT=587,
    )
    adapter = SMTPEmailAdapter(disabled_settings)
    message = EmailMessage(
        message_id="<disabled-event@commonsbook.invalid>",
        to="member@example.com",
        subject="Disabled test",
        text_body="Body",
    )

    with patch("smtplib.SMTP") as mock_smtp_cls:
        with pytest.raises(EmailDeliveryError) as exc_info:
            await adapter.send(message)
        assert exc_info.value.category == "transport"
        mock_smtp_cls.assert_not_called()


@pytest.mark.asyncio
async def test_smtp_adapter_no_body_or_recipient_logging_in_production(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """In production, no recipient email, message body, or SMTP password is logged."""
    prod_settings = Settings.model_construct(
        APP_ENV="production",
        EMAIL_ENABLED=True,
        EMAIL_ADAPTER="smtp",
        SMTP_HOST="smtp.production.invalid",
        SMTP_PORT=587,
        SMTP_USERNAME="prod_user",
        SMTP_PASSWORD="production_super_secret_password_never_log",
        EMAIL_FROM="notifications@commonsbook.invalid",
    )
    adapter = SMTPEmailAdapter(prod_settings)
    message = EmailMessage(
        message_id="<production-event-id@commonsbook.invalid>",
        to="private_user@example.com",
        subject="Private Subject",
        text_body="Confidential text body content that should never appear in production logs.",
    )

    mock_smtp_instance = MagicMock()
    with patch("smtplib.SMTP", return_value=mock_smtp_instance):
        with caplog.at_level(logging.DEBUG, logger="notifications.adapters"):
            returned_id = await adapter.send(message)
            assert returned_id == message.message_id

    for record in caplog.records:
        msg = record.getMessage()
        assert "private_user@example.com" not in msg
        assert "Confidential text body" not in msg
        assert "production_super_secret_password_never_log" not in msg
