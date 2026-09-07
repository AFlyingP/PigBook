import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthorizedScope
from app.bookings.models import Booking
from app.resources.models import Resource


class NotFoundError(Exception):
    def __init__(self, message: str = "Resource not found") -> None:
        self.message = message
        super().__init__(message)


class ResourceInactive(Exception):
    def __init__(self, message: str = "Resource is inactive") -> None:
        self.message = message
        super().__init__(message)


class SlotConflict(Exception):
    def __init__(
        self, message: str = "Slot conflict: requested time interval is not available"
    ) -> None:
        self.message = message
        super().__init__(message)


class InvalidWindowError(Exception):
    def __init__(self, message: str = "Invalid booking window") -> None:
        self.message = message
        super().__init__(message)


def validate_booking_window(
    starts_at: datetime,
    ends_at: datetime,
    now: datetime,
) -> tuple[datetime, datetime]:
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        raise InvalidWindowError("Timestamps must include an explicit timezone offset")

    starts_at_utc = starts_at.astimezone(timezone.utc)
    ends_at_utc = ends_at.astimezone(timezone.utc)

    if (
        starts_at_utc.second != 0
        or starts_at_utc.microsecond != 0
        or starts_at_utc.minute not in (0, 30)
    ):
        raise InvalidWindowError(
            "starts_at must fall on a UTC 30-minute boundary with zero seconds and microseconds"
        )

    if ends_at_utc.second != 0 or ends_at_utc.microsecond != 0 or ends_at_utc.minute not in (0, 30):
        raise InvalidWindowError(
            "ends_at must fall on a UTC 30-minute boundary with zero seconds and microseconds"
        )

    if ends_at_utc <= starts_at_utc:
        raise InvalidWindowError("ends_at must be strictly greater than starts_at")

    duration = ends_at_utc - starts_at_utc
    if duration < timedelta(minutes=30) or duration > timedelta(hours=4):
        raise InvalidWindowError(
            "Booking duration must be between 30 minutes and 4 hours inclusive"
        )

    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)

    if starts_at_utc < now_utc + timedelta(minutes=15):
        raise InvalidWindowError("starts_at must be at least 15 minutes in the future")

    if starts_at_utc > now_utc + timedelta(days=90):
        raise InvalidWindowError("starts_at must be at most 90 days in the future")

    return starts_at_utc, ends_at_utc


async def insert_confirmed(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    resource_id: uuid.UUID,
    time_range: Range,
    kind: str = "reservation",
    now: datetime | None = None,
) -> Booking:
    # 1. Acquire the resource row FOR SHARE and re-read it.
    stmt = select(Resource).where(Resource.id == resource_id).with_for_update(read=True)
    result = await session.execute(stmt)
    resource = result.scalar_one_or_none()

    # 2. Raise a typed NotFoundError if the resource does not exist.
    if resource is None:
        raise NotFoundError(f"Resource {resource_id} not found")

    # 3. Raise a typed ResourceInactive if the resource exists but active is false.
    if not resource.active:
        raise ResourceInactive(f"Resource {resource_id} is inactive")

    # 4. Insert the booking with status confirmed inside a savepoint, and flush().
    booking = Booking(
        id=uuid.uuid4(),
        resource_id=resource_id,
        user_id=scope.principal_id if kind == "reservation" else None,
        created_by=scope.principal_id,
        kind=kind,
        time_range=time_range,
        status="confirmed",
        expires_at=None,
        version=1,
    )

    try:
        async with session.begin_nested():
            session.add(booking)
            await session.flush()
    except IntegrityError as exc:
        orig = getattr(exc, "orig", exc)
        cause = getattr(orig, "__cause__", None) or orig
        sqlstate = (
            getattr(cause, "sqlstate", None)
            or getattr(orig, "sqlstate", None)
            or getattr(orig, "pgcode", None)
        )
        constraint_name = getattr(cause, "constraint_name", None) or getattr(
            orig, "constraint_name", None
        )
        if sqlstate == "23P01" and constraint_name == "bookings_no_overlap":
            raise SlotConflict("Slot conflict: requested time interval is not available") from exc
        raise

    return booking
