"""Unit tests for outbox backoff, jitter, dead-letter boundary, and error redaction.

Ticket: T-018
Spec: 3.4, 6.2
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.notifications.outbox import (
    OutboxLease,
    compute_backoff_seconds,
    fail_lease,
    recover_stale_leases,
    sanitize_error_category,
)


def test_deterministic_exponential_backoff_and_jitter() -> None:
    """Test deterministic exponential backoff formula: min(3600, 5*2^(attempt-1)) + int(id)%5."""
    # Create UUIDs with specific modulo 5 values
    uuid_mod0 = uuid.UUID("00000000-0000-0000-0000-000000000000")  # int is 0 -> 0 % 5 = 0
    uuid_mod1 = uuid.UUID("00000000-0000-0000-0000-000000000001")  # int is 1 -> 1 % 5 = 1
    uuid_mod2 = uuid.UUID("00000000-0000-0000-0000-000000000002")  # int is 2 -> 2 % 5 = 2
    uuid_mod3 = uuid.UUID("00000000-0000-0000-0000-000000000003")  # int is 3 -> 3 % 5 = 3
    uuid_mod4 = uuid.UUID("00000000-0000-0000-0000-000000000004")  # int is 4 -> 4 % 5 = 4

    assert int(uuid_mod0) % 5 == 0
    assert int(uuid_mod1) % 5 == 1
    assert int(uuid_mod2) % 5 == 2
    assert int(uuid_mod3) % 5 == 3
    assert int(uuid_mod4) % 5 == 4

    # Exponential sequence with mod 0:
    # attempt 1: 5 * 2^0 = 5
    # attempt 2: 5 * 2^1 = 10
    # attempt 3: 5 * 2^2 = 20
    # attempt 4: 5 * 2^3 = 40
    # attempt 5: 5 * 2^4 = 80
    # attempt 6: 5 * 2^5 = 160
    # attempt 7: 5 * 2^6 = 320
    assert compute_backoff_seconds(uuid_mod0, 1) == 5
    assert compute_backoff_seconds(uuid_mod0, 2) == 10
    assert compute_backoff_seconds(uuid_mod0, 3) == 20
    assert compute_backoff_seconds(uuid_mod0, 4) == 40
    assert compute_backoff_seconds(uuid_mod0, 5) == 80
    assert compute_backoff_seconds(uuid_mod0, 6) == 160
    assert compute_backoff_seconds(uuid_mod0, 7) == 320

    # Exponential sequence with mod 3 jitter:
    assert compute_backoff_seconds(uuid_mod3, 1) == 5 + 3
    assert compute_backoff_seconds(uuid_mod3, 2) == 10 + 3
    assert compute_backoff_seconds(uuid_mod3, 3) == 20 + 3
    assert compute_backoff_seconds(uuid_mod3, 7) == 320 + 3


def test_backoff_ceiling_cap_at_3600() -> None:
    """Base backoff must be capped at 3600 seconds (1 hour) plus jitter."""
    event_id = uuid.UUID("00000000-0000-0000-0000-000000000002")  # jitter = 2
    # 5 * 2^9 = 2560
    assert compute_backoff_seconds(event_id, 10) == 2560 + 2
    # 5 * 2^10 = 5120 -> capped to 3600
    assert compute_backoff_seconds(event_id, 11) == 3600 + 2
    # attempts well beyond cap
    assert compute_backoff_seconds(event_id, 20) == 3600 + 2
    assert compute_backoff_seconds(event_id, 100) == 3600 + 2


def test_sanitize_error_category_length_cap() -> None:
    """Error categories must be capped at 500 characters."""
    long_error = "a" * 600
    sanitized = sanitize_error_category(long_error)
    assert len(sanitized) == 500
    assert sanitized == "a" * 500


def test_sanitize_error_category_redacts_credentials() -> None:
    """Secrets, passwords, and tokens must be redacted from error messages."""
    raw = "SMTP authentication failed: password=supersecretpass for user admin"
    sanitized = sanitize_error_category(raw)
    assert "supersecretpass" not in sanitized
    assert "password=[REDACTED]" in sanitized

    bearer_raw = "Failed transport Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.test"
    sanitized_bearer = sanitize_error_category(bearer_raw)
    assert "eyJhbGciOiJIUzI1NiJ9" not in sanitized_bearer
    assert "Bearer [REDACTED]" in sanitized_bearer

    token_raw = "Connection rejected token: secret_api_token_12345"
    assert "secret_api_token_12345" not in sanitize_error_category(token_raw)


def test_sanitize_error_category_strips_control_characters() -> None:
    """Control characters must be sanitized to prevent log or terminal corruption."""
    raw = "Error\x00with\x1b[31mANSI\x07and bells"
    sanitized = sanitize_error_category(raw)
    assert "\x00" not in sanitized
    assert "\x1b" not in sanitized
    assert "\x07" not in sanitized


@pytest.mark.asyncio
async def test_fail_lease_dead_letter_boundary_at_eight_attempts() -> None:
    """At attempts >= 8, fail_lease must mark status='dead' instead of 'pending'."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.rowcount = 1
    mock_session.execute.return_value = mock_result

    now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
    event_id = uuid.uuid4()
    lease_token = uuid.uuid4()

    # Attempt 7 (under 8): should transition to pending
    lease_7 = OutboxLease(
        id=event_id,
        lease_token=lease_token,
        event_type="booking_confirmed",
        payload={},
        attempts=7,
    )
    res_7 = await fail_lease(mock_session, lease=lease_7, error_category="timeout", now=now)
    assert res_7 is True
    call_stmt_7 = mock_session.execute.call_args[0][0]
    # Check values updated for attempt 7
    compiled_params_7 = call_stmt_7.compile().params
    assert compiled_params_7["status"] == "pending"

    # Attempt 8 (boundary): should transition to dead
    lease_8 = OutboxLease(
        id=event_id,
        lease_token=lease_token,
        event_type="booking_confirmed",
        payload={},
        attempts=8,
    )
    res_8 = await fail_lease(mock_session, lease=lease_8, error_category="transport", now=now)
    assert res_8 is True
    call_stmt_8 = mock_session.execute.call_args[0][0]
    compiled_params_8 = call_stmt_8.compile().params
    assert compiled_params_8["status"] == "dead"

    # Attempt 9: should transition to dead
    lease_9 = OutboxLease(
        id=event_id,
        lease_token=lease_token,
        event_type="booking_confirmed",
        payload={},
        attempts=9,
    )
    res_9 = await fail_lease(mock_session, lease=lease_9, error_category="transport", now=now)
    assert res_9 is True
    call_stmt_9 = mock_session.execute.call_args[0][0]
    compiled_params_9 = call_stmt_9.compile().params
    assert compiled_params_9["status"] == "dead"


@pytest.mark.asyncio
async def test_stale_lease_recovery_dead_letter_boundary() -> None:
    """Stale lease recovery executes updates returning rows < 8 to pending, and >= 8 to dead."""
    mock_session = AsyncMock()

    mock_res_dead = MagicMock()
    mock_res_dead.rowcount = 1

    mock_res_pending = MagicMock()
    mock_res_pending.rowcount = 1

    mock_session.execute = AsyncMock(side_effect=[mock_res_dead, mock_res_pending])

    now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
    recovered = await recover_stale_leases(mock_session, now=now)

    assert recovered == 2
    assert mock_session.execute.call_count == 2
