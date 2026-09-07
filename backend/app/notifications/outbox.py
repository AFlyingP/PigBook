import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.bookings.models import Booking
from app.notifications.models import Outbox


def _format_rfc3339_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
