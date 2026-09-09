import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bookings.models import Booking
from app.notifications.models import Outbox


@dataclass(frozen=True)
class OutboxLease:
    id: uuid.UUID
    lease_token: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    attempts: int


def _format_rfc3339_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def compute_backoff_seconds(event_id: uuid.UUID, attempt: int) -> int:
    """Calculate exponential backoff with deterministic jitter (Spec 6.2).

    Formula: min(3600, 5 * 2^(attempt - 1)) + (int(event_id) % 5) seconds.
    """
    safe_attempt = max(1, attempt)
    base = min(3600, 5 * (2 ** (safe_attempt - 1)))
    jitter = int(event_id) % 5
    return base + jitter


def sanitize_error_category(error: str) -> str:
    """Redact secrets/credentials and cap error message to 500 characters.

    Ensures no passwords, tokens, full message bodies, or auth headers leak
    into outbox.last_error.
    """
    if not error:
        return ""
    redacted = re.sub(
        r"(?i)(password|passwd|pwd|secret|token|auth)\s*[:=]\s*\S+", r"\1=[REDACTED]", error
    )
    redacted = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [REDACTED]", redacted)
    cleaned = "".join(ch if (ch >= " " or ch in "\n\t") else " " for ch in redacted)
    return cleaned[:500].strip()


async def append_event(
    session: AsyncSession,
    *,
    event_type: str,
    booking: Booking,
    now: datetime | None = None,
) -> uuid.UUID:
    """Append an event to the outbox table within the caller's transaction."""
    starts_at = _format_rfc3339_utc(booking.time_range.lower)
    ends_at = _format_rfc3339_utc(booking.time_range.upper)
    expires_at = _format_rfc3339_utc(getattr(booking, "expires_at", None))

    payload = {
        "schema_version": 1,
        "booking_id": str(booking.id),
        "recipient_id": str(booking.user_id) if booking.user_id is not None else None,
        "resource_id": str(booking.resource_id),
        "starts_at": starts_at,
        "ends_at": ends_at,
        "expires_at": expires_at,
    }

    event_id = uuid.uuid4()
    kwargs = {
        "id": event_id,
        "event_type": event_type,
        "aggregate_id": booking.id,
        "aggregate_version": booking.version,
        "payload": payload,
        "status": "pending",
    }
    if now is not None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        kwargs["occurred_at"] = now
        kwargs["available_at"] = now

    event = Outbox(**kwargs)
    session.add(event)
    await session.flush()
    return event_id


async def claim_batch(
    session: AsyncSession,
    *,
    now: datetime,
) -> list[OutboxLease]:
    """Claim at most one due row per idle dispatcher (Spec 3.4, 6.1).

    Ordered by available_at, occurred_at, id using SELECT FOR UPDATE SKIP LOCKED.
    Sets status='processing', attempts = attempts + 1, fresh lease_token UUID,
    and lease_until = now + 60s. Commits before dispatch.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    stmt = (
        select(Outbox)
        .where(
            Outbox.status == "pending",
            Outbox.available_at <= now,
        )
        .order_by(
            Outbox.available_at.asc(),
            Outbox.occurred_at.asc(),
            Outbox.id.asc(),
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        return []

    r = cast(Any, row)
    new_token = uuid.uuid4()
    new_attempts = int(r.attempts) + 1
    lease_until = now + timedelta(seconds=60)

    r.status = "processing"
    r.attempts = new_attempts
    r.lease_token = new_token
    r.lease_until = lease_until

    lease = OutboxLease(
        id=uuid.UUID(str(r.id)),
        lease_token=new_token,
        event_type=str(r.event_type),
        payload=dict(r.payload),
        attempts=new_attempts,
    )

    try:
        await session.commit()
    except Exception:
        await session.flush()

    return [lease]


async def renew_lease(
    session: AsyncSession,
    *,
    lease: OutboxLease,
    now: datetime,
) -> bool:
    """Renew the owned lease immediately before dispatch (Spec 6.1).

    Returns True if renewed, False if ownership was lost.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    new_until = now + timedelta(seconds=60)
    stmt = (
        update(Outbox)
        .where(
            Outbox.id == lease.id,
            Outbox.lease_token == lease.lease_token,
            Outbox.status == "processing",
        )
        .values(
            lease_until=new_until,
        )
    )
    result = await session.execute(stmt)
    owned = getattr(result, "rowcount", None) == 1
    if owned:
        try:
            await session.commit()
        except Exception:
            await session.flush()
    return owned


async def acknowledge(
    session: AsyncSession,
    *,
    lease: OutboxLease,
    now: datetime,
) -> bool:
    """Token-fenced completion marking outbox row as delivered (Spec 3.4, 6.1).

    Includes matching lease_token and status='processing'.
    Sets status='delivered', delivered_at=now, clears lease fields.
    Returns True if owned, False otherwise.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    stmt = (
        update(Outbox)
        .where(
            Outbox.id == lease.id,
            Outbox.lease_token == lease.lease_token,
            Outbox.status == "processing",
        )
        .values(
            status="delivered",
            delivered_at=now,
            lease_token=None,
            lease_until=None,
        )
    )
    result = await session.execute(stmt)
    owned = getattr(result, "rowcount", None) == 1
    if owned:
        try:
            await session.commit()
        except Exception:
            await session.flush()
    return owned


async def fail_lease(
    session: AsyncSession,
    *,
    lease: OutboxLease,
    error_category: str,
    now: datetime,
) -> bool:
    """Token-fenced failure handler with exponential backoff or dead-lettering (Spec 3.4, 6.2).

    Includes matching lease_token and status='processing'.
    At attempts >= 8: transitions to 'dead'.
    At attempts < 8: returns to 'pending' with available_at = now + backoff.
    Clears lease fields, stores sanitized error_category (max 500 chars).
    Returns True if owned, False otherwise.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    sanitized = sanitize_error_category(error_category)
    if lease.attempts >= 8:
        new_status = "dead"
        new_available = now
    else:
        new_status = "pending"
        backoff = compute_backoff_seconds(lease.id, lease.attempts)
        new_available = now + timedelta(seconds=backoff)

    stmt = (
        update(Outbox)
        .where(
            Outbox.id == lease.id,
            Outbox.lease_token == lease.lease_token,
            Outbox.status == "processing",
        )
        .values(
            status=new_status,
            available_at=new_available,
            lease_token=None,
            lease_until=None,
            last_error=sanitized,
        )
    )
    result = await session.execute(stmt)
    owned = getattr(result, "rowcount", None) == 1
    if owned:
        try:
            await session.commit()
        except Exception:
            await session.flush()
    return owned


async def recover_stale_leases(
    session: AsyncSession,
    *,
    now: datetime,
) -> int:
    """Stale-lease recovery for expired processing rows (Spec 6.2).

    Processing rows with lease_until <= now return to 'pending'
    (unless attempts >= 8, which transition to 'dead').
    Safe with concurrent workers using FOR UPDATE SKIP LOCKED.
    Returns the number of rows recovered.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    stmt = (
        select(Outbox)
        .where(
            Outbox.status == "processing",
            Outbox.lease_until <= now,
        )
        .order_by(Outbox.lease_until.asc(), Outbox.id.asc())
        .limit(100)
        .with_for_update(skip_locked=True)
    )
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return 0

    recovered_count = len(rows)
    for row in rows:
        r = cast(Any, row)
        if int(r.attempts) >= 8:
            r.status = "dead"
        else:
            r.status = "pending"
            r.available_at = now
        r.lease_token = None
        r.lease_until = None

    try:
        await session.commit()
    except Exception:
        await session.flush()

    return recovered_count
