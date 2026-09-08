import uuid
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthorizedScope
from app.auth.models import User
from app.bookings.models import Booking
from app.bookings.schemas import StoredResponse
from app.bookings.service import (
    NotFoundError,
    ResourceInactive,
    validate_booking_window,
)
from app.notifications.outbox import append_event
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
    """Hold resource FOR UPDATE throughout release and promotion (Spec 5.1, 5.3).

    Scan all waiting entries for that resource in ascending (created_at, id)
    using keyset pages of 100 inside this transaction.
    """
    # 1. Hold the resource FOR UPDATE
    await session.execute(select(Resource.id).where(Resource.id == resource_id).with_for_update())

    page_size = 100
    last_created_at: datetime | None = None
    last_id: uuid.UUID | None = None
    promoted_booking_ids: list[uuid.UUID] = []

    while True:
        query = select(WaitlistEntry).where(
            WaitlistEntry.resource_id == resource_id,
            WaitlistEntry.status == "waiting",
        )
        if last_created_at is not None and last_id is not None:
            query = query.where(
                or_(
                    WaitlistEntry.created_at > last_created_at,
                    and_(
                        WaitlistEntry.created_at == last_created_at,
                        WaitlistEntry.id > last_id,
                    ),
                )
            )
        query = query.order_by(
            WaitlistEntry.created_at.asc(),
            WaitlistEntry.id.asc(),
        ).limit(page_size)

        entries = (await session.execute(query)).scalars().all()
        if not entries:
            break

        for entry in entries:
            last_created_at = cast(datetime, entry.created_at)
            last_id = cast(uuid.UUID, entry.id)

            # Sample database clock
            db_clock = await session.scalar(select(func.clock_timestamp()))
            db_now = db_clock if db_clock is not None else now

            # Check owner enabled
            owner_stmt = select(User.enabled).where(User.id == entry.user_id)
            owner_enabled = (await session.execute(owner_stmt)).scalar_one_or_none()
            if owner_enabled is None or not owner_enabled:
                await session.execute(
                    update(WaitlistEntry)
                    .where(WaitlistEntry.id == entry.id, WaitlistEntry.version == entry.version)
                    .values(
                        status="expired",
                        version=WaitlistEntry.version + 1,
                        updated_at=db_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                continue

            # Check starts_at < db_time + 15 minutes
            if entry.time_range.lower < db_now + timedelta(minutes=15):
                await session.execute(
                    update(WaitlistEntry)
                    .where(WaitlistEntry.id == entry.id, WaitlistEntry.version == entry.version)
                    .values(
                        status="expired",
                        version=WaitlistEntry.version + 1,
                        updated_at=db_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                continue

            # Check if entire requested window is free
            overlap_stmt = select(Booking.id).where(
                Booking.resource_id == resource_id,
                Booking.status.in_(("confirmed", "offered")),
                Booking.time_range.op("&&")(entry.time_range),
            )
            has_overlap = (await session.execute(overlap_stmt)).first() is not None
            if has_overlap:
                # FIFO per exact window: blocked older window does not block younger disjoint window
                continue

            # Sample clock_timestamp() immediately before each offer
            offer_clock = await session.scalar(select(func.clock_timestamp()))
            offer_now = offer_clock if offer_clock is not None else db_now

            offer_expires_at = offer_now + timedelta(minutes=15)
            if offer_expires_at > entry.time_range.lower:
                offer_expires_at = entry.time_range.lower

            offered_booking = Booking(
                id=uuid.uuid4(),
                resource_id=resource_id,
                user_id=entry.user_id,
                created_by=entry.user_id,
                kind="reservation",
                time_range=entry.time_range,
                status="offered",
                expires_at=offer_expires_at,
                version=1,
            )

            # Savepoint handling: 23P01 on bookings_no_overlap from an independent writer
            # rolls back only that offer's savepoint, leaves that entry waiting, and continues.
            conflict = False
            try:
                async with session.begin_nested():
                    session.add(offered_booking)
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
                    conflict = True
                else:
                    raise

            if conflict:
                continue

            # Success: link booking to entry, update entry to offered, append event
            await session.execute(
                update(WaitlistEntry)
                .where(WaitlistEntry.id == entry.id, WaitlistEntry.version == entry.version)
                .values(
                    status="offered",
                    offered_booking_id=offered_booking.id,
                    version=WaitlistEntry.version + 1,
                    updated_at=offer_now,
                )
                .execution_options(synchronize_session=False)
            )

            await append_event(
                session,
                event_type="waitlist_offered",
                booking=offered_booking,
                now=offer_now,
            )
            promoted_booking_ids.append(cast(uuid.UUID, offered_booking.id))

        if len(entries) < page_size:
            break

    return promoted_booking_ids


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
