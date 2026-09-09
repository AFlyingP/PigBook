"""Unit tests for email notification templates (Spec 6.3).

Ticket: T-018
"""

import re
from datetime import datetime, timezone

from app.notifications.templates import (
    escape_control_characters,
    format_time_pair,
    render_booking_cancelled,
    render_booking_confirmed,
    render_hold_expired,
    render_waitlist_offered,
)


def test_escape_control_characters() -> None:
    """Control characters and terminal codes must be escaped or stripped."""
    raw = "Study Room\x00\x1b[31m A\r\n\t#1"
    escaped = escape_control_characters(raw)
    assert "\x00" not in escaped
    assert "\x1b" not in escaped
    assert "Study Room" in escaped
    assert "A" in escaped


def test_format_time_pair() -> None:
    """format_time_pair must output both UTC and America/New_York local times."""
    dt = datetime(2026, 7, 15, 14, 30, 0, tzinfo=timezone.utc)
    utc_str, local_str = format_time_pair(dt)

    assert "2026-07-15 14:30:00 UTC" == utc_str
    # In July, EDT is UTC-4 -> 10:30:00 EDT
    assert "2026-07-15 10:30:00 EDT (America/New_York)" == local_str


def test_render_booking_confirmed() -> None:
    """render_booking_confirmed includes resource name, times, and /my-bookings link."""
    starts = datetime(2026, 9, 10, 14, 0, 0, tzinfo=timezone.utc)
    ends = datetime(2026, 9, 10, 16, 0, 0, tzinfo=timezone.utc)

    subject, body = render_booking_confirmed(
        resource_name="Main Hall\x00",
        starts_at=starts,
        ends_at=ends,
        app_origin="https://commonsbook.example.com",
    )

    assert subject == "Booking Confirmed: Main Hall"
    assert "\x00" not in subject
    assert "Your booking for Main Hall is confirmed." in body
    assert "14:00:00 UTC" in body
    assert "America/New_York" in body
    assert "https://commonsbook.example.com/my-bookings" in body
    # Never include auth tokens
    assert not re.search(r"(?i)(token|bearer|password)", body)


def test_render_booking_cancelled() -> None:
    """render_booking_cancelled includes resource name, times, and /my-bookings link."""
    starts = datetime(2026, 9, 10, 14, 0, 0, tzinfo=timezone.utc)
    ends = datetime(2026, 9, 10, 16, 0, 0, tzinfo=timezone.utc)

    subject, body = render_booking_cancelled(
        resource_name="Conference Room B",
        starts_at=starts,
        ends_at=ends,
        app_origin="https://commonsbook.example.com",
    )

    assert subject == "Booking Cancelled: Conference Room B"
    assert "cancelled" in body
    assert "Conference Room B" in body
    assert "https://commonsbook.example.com/my-bookings" in body
    assert not re.search(r"(?i)(token|bearer|password)", body)


def test_render_waitlist_offered() -> None:
    """render_waitlist_offered includes interval, deadline, and /waitlist link."""
    starts = datetime(2026, 9, 10, 14, 0, 0, tzinfo=timezone.utc)
    ends = datetime(2026, 9, 10, 16, 0, 0, tzinfo=timezone.utc)
    expires = datetime(2026, 9, 10, 10, 15, 0, tzinfo=timezone.utc)

    subject, body = render_waitlist_offered(
        resource_name="Quiet Pod #3",
        starts_at=starts,
        ends_at=ends,
        expires_at=expires,
        app_origin="https://commonsbook.example.com",
    )

    assert subject == "Waitlist Offer: Quiet Pod #3"
    assert "Offer Expiry Deadline:" in body
    assert "10:15:00 UTC" in body
    assert "https://commonsbook.example.com/waitlist" in body
    assert not re.search(r"(?i)(token|bearer|password)", body)


def test_render_hold_expired() -> None:
    """render_hold_expired includes resource name, interval, and /waitlist link."""
    starts = datetime(2026, 9, 10, 14, 0, 0, tzinfo=timezone.utc)
    ends = datetime(2026, 9, 10, 16, 0, 0, tzinfo=timezone.utc)

    subject, body = render_hold_expired(
        resource_name="Lab Bench A",
        starts_at=starts,
        ends_at=ends,
        app_origin="https://commonsbook.example.com",
    )

    assert subject == "Waitlist Hold Expired: Lab Bench A"
    assert "expired" in body
    assert "Lab Bench A" in body
    assert "https://commonsbook.example.com/waitlist" in body
    assert not re.search(r"(?i)(token|bearer|password)", body)
