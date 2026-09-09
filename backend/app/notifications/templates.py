"""Plain-text email notification templates (Spec 6.3).

Covers booking_confirmed, booking_cancelled, waitlist_offered, and hold_expired.
Each template provides resource name, UTC and organization-local times (America/New_York),
and links to /my-bookings or /waitlist. Control characters are escaped and auth tokens
are never included.
"""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ORG_TIMEZONE = ZoneInfo("America/New_York")


def escape_control_characters(text: str) -> str:
    """Strip or escape non-printable control characters to prevent header or terminal injection."""
    if not text:
        return ""
    # Strip C0/C1 control chars except standard newlines and tabs
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", " ", text)
    return cleaned.strip()


def escape_header_value(text: str) -> str:
    """Strip CR, LF, and all other control characters to prevent email header injection (R2)."""
    if not text:
        return ""
    # Strip CR (\r), LF (\n), and all other control characters
    cleaned = re.sub(r"[\r\n\x00-\x1f\x7f-\x9f]", " ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def format_time_pair(dt: datetime) -> tuple[str, str]:
    """Format datetime in UTC and America/New_York."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc_dt = dt.astimezone(timezone.utc)
    local_dt = dt.astimezone(ORG_TIMEZONE)

    utc_str = utc_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    local_str = local_dt.strftime("%Y-%m-%d %H:%M:%S %Z (America/New_York)")
    return utc_str, local_str


def render_booking_confirmed(
    *,
    resource_name: str,
    starts_at: datetime,
    ends_at: datetime,
    app_origin: str = "http://localhost:5173",
) -> tuple[str, str]:
    """Render subject and body for booking_confirmed notification."""
    header_resource = escape_header_value(resource_name)
    safe_resource = escape_control_characters(resource_name)
    subject = f"Booking Confirmed: {header_resource}"

    start_utc, start_local = format_time_pair(starts_at)
    end_utc, end_local = format_time_pair(ends_at)
    link = f"{app_origin.rstrip('/')}/my-bookings"

    body = (
        f"Your booking for {safe_resource} is confirmed.\n\n"
        f"Starts At:\n"
        f"  UTC:   {start_utc}\n"
        f"  Local: {start_local}\n\n"
        f"Ends At:\n"
        f"  UTC:   {end_utc}\n"
        f"  Local: {end_local}\n\n"
        f"View your bookings at: {link}\n"
    )
    return subject, body


def render_booking_cancelled(
    *,
    resource_name: str,
    starts_at: datetime,
    ends_at: datetime,
    app_origin: str = "http://localhost:5173",
) -> tuple[str, str]:
    """Render subject and body for booking_cancelled notification."""
    header_resource = escape_header_value(resource_name)
    safe_resource = escape_control_characters(resource_name)
    subject = f"Booking Cancelled: {header_resource}"

    start_utc, start_local = format_time_pair(starts_at)
    end_utc, end_local = format_time_pair(ends_at)
    link = f"{app_origin.rstrip('/')}/my-bookings"

    body = (
        f"Your booking for {safe_resource} has been cancelled.\n\n"
        f"Original Interval:\n"
        f"  Starts: {start_utc} ({start_local})\n"
        f"  Ends:   {end_utc} ({end_local})\n\n"
        f"View your bookings at: {link}\n"
    )
    return subject, body


def render_waitlist_offered(
    *,
    resource_name: str,
    starts_at: datetime,
    ends_at: datetime,
    expires_at: datetime,
    app_origin: str = "http://localhost:5173",
) -> tuple[str, str]:
    """Render subject and body for waitlist_offered notification."""
    header_resource = escape_header_value(resource_name)
    safe_resource = escape_control_characters(resource_name)
    subject = f"Waitlist Offer: {header_resource}"

    start_utc, start_local = format_time_pair(starts_at)
    end_utc, end_local = format_time_pair(ends_at)
    exp_utc, exp_local = format_time_pair(expires_at)
    link = f"{app_origin.rstrip('/')}/waitlist"

    body = (
        f"A reservation slot for {safe_resource} is now available!\n\n"
        f"Slot Interval:\n"
        f"  Starts: {start_utc} ({start_local})\n"
        f"  Ends:   {end_utc} ({end_local})\n\n"
        f"Offer Expiry Deadline:\n"
        f"  UTC:   {exp_utc}\n"
        f"  Local: {exp_local}\n\n"
        f"To accept or decline this offer before it expires, visit: {link}\n"
    )
    return subject, body


def render_hold_expired(
    *,
    resource_name: str,
    starts_at: datetime,
    ends_at: datetime,
    app_origin: str = "http://localhost:5173",
) -> tuple[str, str]:
    """Render subject and body for hold_expired notification."""
    header_resource = escape_header_value(resource_name)
    safe_resource = escape_control_characters(resource_name)
    subject = f"Waitlist Hold Expired: {header_resource}"

    start_utc, start_local = format_time_pair(starts_at)
    end_utc, end_local = format_time_pair(ends_at)
    link = f"{app_origin.rstrip('/')}/waitlist"

    body = (
        f"Your waitlist offer hold for {safe_resource} has expired because "
        f"it was not accepted within the response deadline.\n\n"
        f"Slot Interval:\n"
        f"  Starts: {start_utc} ({start_local})\n"
        f"  Ends:   {end_utc} ({end_local})\n\n"
        f"Check waitlist status at: {link}\n"
    )
    return subject, body
