import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthorizedScope
from app.bookings.models import Booking
from app.bookings.schemas import StoredResponse
from app.bookings.service import (
    NotFoundError,
    ResourceInactive,
    validate_booking_window,
)
from app.resources.models import Resource
from app.resources.schemas import Page
from app.waitlist.models import WaitlistEntry
from app.waitlist.schemas import WaitCreate, WaitEntry


class SlotAvailable(Exception):
    def __init__(
        self, message: str = "Slot is available for direct booking without waitlist"
    ) -> None:
        self.message = message
        super().__init__(message)


class AlreadyWaitlisted(Exception):
    def __init__(self, message: str = "User is already on the waitlist for this window") -> None:
        self.message = message
        super().__init__(message)


class AlreadyBooked(Exception):
    def __init__(
        self,
        message: str = "User already owns an active reservation overlapping this interval",
    ) -> None:
        self.message = message
        super().__init__(message)


class WaitlistFull(Exception):
    def __init__(
        self,
        message: str = "The waitlist for this resource is currently at full capacity",
    ) -> None:
        self.message = message
        super().__init__(message)


class HoldExpired(Exception):
    def __init__(self, message: str = "The offer for this booking has expired") -> None:
        self.message = message
        super().__init__(message)


async def join_waitlist(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    data: WaitCreate,
    now: datetime,
) -> WaitEntry:
    """Join the waitlist for a resource window.

    Holds the resource row FOR UPDATE throughout membership changes (Spec 5.1).
    Enforces active booking/blackout overlap requirement, own-overlap rejection,
    exact-window duplicate rejection, and the 500-entry capacity limit.
    """
    if scope.principal_id is None:
        raise RuntimeError("Authenticated scope required for join_waitlist")

    # 1. Validate window policy
    starts_at_utc, ends_at_utc = validate_booking_window(data.starts_at, data.ends_at, now)
    desired_range = Range(starts_at_utc, ends_at_utc, bounds="[)")

    # 2. Lock resource FOR UPDATE and verify active
    stmt = select(Resource).where(Resource.id == data.resource_id).with_for_update()
    resource = (await session.execute(stmt)).scalar_one_or_none()
    if resource is None:
        raise NotFoundError(f"Resource {data.resource_id} not found")
    if not resource.active:
        raise ResourceInactive(f"Resource {data.resource_id} is inactive")

    # 3. Check active booking/blackout overlaps the desired interval
    stmt_overlap = select(Booking).where(
        Booking.resource_id == data.resource_id,
        Booking.status.in_(("confirmed", "offered")),
        Booking.time_range.op("&&")(desired_range),
    )
    overlapping_bookings = (await session.execute(stmt_overlap)).scalars().all()
    if not overlapping_bookings:
        raise SlotAvailable("Requested slot is available for direct booking")

    # 4. Check if the caller already owns an overlapping active reservation
    for booking in overlapping_bookings:
        if booking.kind == "reservation" and booking.user_id == scope.principal_id:
            raise AlreadyBooked("User already owns an active reservation overlapping this interval")

    # 5. Check exact-window active membership uniqueness before capacity check
    stmt_existing = select(WaitlistEntry.id).where(
        WaitlistEntry.user_id == scope.principal_id,
        WaitlistEntry.resource_id == data.resource_id,
        WaitlistEntry.time_range == desired_range,
        WaitlistEntry.status.in_(("waiting", "offered")),
    )
    if (await session.execute(stmt_existing)).scalar_one_or_none() is not None:
        raise AlreadyWaitlisted("User is already on the waitlist for this window")

    # 6. Capacity limit: 500 active (waiting + offered) entries per resource
    stmt_count = (
        select(func.count())
        .select_from(WaitlistEntry)
        .where(
            WaitlistEntry.resource_id == data.resource_id,
            WaitlistEntry.status.in_(("waiting", "offered")),
        )
    )
    active_count = (await session.execute(stmt_count)).scalar_one()
    if active_count >= 500:
        raise WaitlistFull(
            "The waitlist for this resource is currently full (maximum 500 active entries)"
        )

    # 7. Insert waitlist entry inside savepoint
    entry = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=scope.principal_id,
        resource_id=data.resource_id,
        time_range=desired_range,
        status="waiting",
        offered_booking_id=None,
        version=1,
    )

    try:
        async with session.begin_nested():
            session.add(entry)
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
        if sqlstate == "23505" and constraint_name == "waitlist_active_unique_idx":
            raise AlreadyWaitlisted("User is already on the waitlist for this window") from exc
        raise

    await session.refresh(entry)
    return WaitEntry.model_validate(entry)


async def list_own_waitlist(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    limit: int = 25,
    offset: int = 0,
    status: str | None = None,
) -> Page[WaitEntry]:
    """List the caller's own waitlist entries, ordered created_at descending then id."""
    waitlist_predicates = scope.predicates.get("waitlist", ())
    if not isinstance(waitlist_predicates, (list, tuple)):
        waitlist_predicates = (waitlist_predicates,)

    conditions = [*waitlist_predicates]
    if status is not None:
        conditions.append(WaitlistEntry.status == status)

    count_stmt = select(func.count()).select_from(WaitlistEntry).where(*conditions)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(WaitlistEntry)
        .where(*conditions)
        .order_by(WaitlistEntry.created_at.desc(), WaitlistEntry.id.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()

    return Page[WaitEntry](
        items=[WaitEntry.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


async def promote_waiters(
    session: AsyncSession,
    resource_id: uuid.UUID,
    now: datetime,
) -> list[uuid.UUID]:
    # Placeholder for Phase B
    return []


async def accept_offer(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    entry_id: uuid.UUID,
    expected_version: int,
    now: datetime,
) -> StoredResponse:
    # Placeholder for Phase C
    raise NotImplementedError()


async def decline_entry(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    entry_id: uuid.UUID,
    expected_version: int,
    now: datetime,
) -> WaitEntry:
    # Placeholder for Phase C
    raise NotImplementedError()
