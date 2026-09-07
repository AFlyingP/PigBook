import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthorizedScope
from app.bookings.models import Booking
from app.resources.models import Resource as ResourceModel
from app.resources.schemas import Availability, OccupiedInterval, Page, Resource


class NotFoundError(Exception):
    def __init__(self, message: str = "Resource not found") -> None:
        self.message = message
        super().__init__(message)


class InvalidWindowError(Exception):
    def __init__(self, message: str = "Invalid availability window") -> None:
        self.message = message
        super().__init__(message)


def parse_and_validate_timestamp(val: str | datetime) -> datetime:
    """Parse and validate an explicit-offset RFC3339 timestamp on a UTC 30-minute boundary."""
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val)
        except (ValueError, TypeError) as exc:
            raise InvalidWindowError("Invalid timestamp format") from exc
    elif isinstance(val, datetime):
        dt = val
    else:
        raise InvalidWindowError("Invalid timestamp type")

    if dt.tzinfo is None:
        raise InvalidWindowError("Timestamp must have explicit timezone offset")

    dt_utc = dt.astimezone(timezone.utc)
    if dt_utc.second != 0 or dt_utc.microsecond != 0 or dt_utc.minute not in (0, 30):
        raise InvalidWindowError(
            "Timestamp must fall on a UTC 30-minute boundary with zero seconds and microseconds"
        )

    return dt_utc


def validate_window(
    starts_at: str | datetime,
    ends_at: str | datetime,
) -> tuple[datetime, datetime]:
    """Validate an availability window satisfying all requirements of Decision B."""
    s_utc = parse_and_validate_timestamp(starts_at)
    e_utc = parse_and_validate_timestamp(ends_at)

    if e_utc <= s_utc:
        raise InvalidWindowError("ends_at must be strictly greater than starts_at")

    if (e_utc - s_utc) > timedelta(days=7):
        raise InvalidWindowError("Window duration cannot exceed 7 days")

    return s_utc, e_utc


async def _get_active_resource(
    session: AsyncSession,
    resource_id: uuid.UUID,
) -> ResourceModel:
    """Retrieve an active resource or raise NotFoundError."""
    stmt = select(ResourceModel).where(
        ResourceModel.id == resource_id,
        ResourceModel.active.is_(True),
    )
    result = await session.execute(stmt)
    resource = result.scalar_one_or_none()
    if resource is None:
        raise NotFoundError("Resource not found")
    return resource


async def list_resources(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    limit: int = 25,
    offset: int = 0,
) -> Page[Resource]:
    """List active resources ordered by name ascending, then id ascending."""
    count_stmt = (
        select(func.count()).select_from(ResourceModel).where(ResourceModel.active.is_(True))
    )
    count_result = await session.execute(count_stmt)
    total = count_result.scalar_one()

    stmt = (
        select(ResourceModel)
        .where(ResourceModel.active.is_(True))
        .order_by(ResourceModel.name.asc(), ResourceModel.id.asc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    resources = result.scalars().all()
    items = [Resource.model_validate(r) for r in resources]

    return Page[Resource](
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def availability(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    resource_id: uuid.UUID,
    starts_at: datetime,
    ends_at: datetime,
) -> Availability:
    """Query occupied booking intervals for an active resource within the given window."""
    s_utc, e_utc = validate_window(starts_at, ends_at)
    await _get_active_resource(session, resource_id)

    query_range = func.tstzrange(s_utc, e_utc, "[)")
    stmt = (
        select(
            func.lower(Booking.time_range).label("starts_at"),
            func.upper(Booking.time_range).label("ends_at"),
            Booking.kind,
            Booking.status,
        )
        .where(
            Booking.resource_id == resource_id,
            Booking.status.in_(["confirmed", "offered"]),
            Booking.time_range.op("&&")(query_range),
        )
        .order_by(
            func.lower(Booking.time_range).asc(),
            Booking.id.asc(),
        )
    )
    result = await session.execute(stmt)
    occupied_rows = result.all()

    occupied = [
        OccupiedInterval(
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            kind=row.kind,
            status=row.status,
        )
        for row in occupied_rows
    ]

    return Availability(
        resource_id=resource_id,
        starts_at=s_utc,
        ends_at=e_utc,
        timezone="America/New_York",
        occupied=occupied,
    )
