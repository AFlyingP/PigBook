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
from app.bookings.schemas import Booking as BookingSchema
from app.bookings.schemas import StoredResponse
from app.bookings.service import (
    InvalidState,
    NotFoundError,
    ResourceInactive,
    TooLate,
    VersionMismatch,
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


async def _list_own_waitlist(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    limit: int = 25,
    offset: int = 0,
    status: str | None = None,
) -> Page[WaitEntry]:
    """List the caller's own waitlist entries, ordered created_at descending then id."""
    waitlist_predicates = scope.predicates.get("waitlist")
    if not waitlist_predicates:
        raise RuntimeError("Owner-scoped waitlist operation requires dependency-supplied predicate")
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

            # Atomic promotion attempt: booking insert, entry update, and event
            # are executed in the same savepoint. If the entry version moved or a 23P01
            # exclusion conflict occurs, the whole attempt rolls back (Spec 5.3, R10).
            conflict = False
            try:
                async with session.begin_nested():
                    session.add(offered_booking)
                    await session.flush()

                    update_res = await session.execute(
                        update(WaitlistEntry)
                        .where(
                            WaitlistEntry.id == entry.id,
                            WaitlistEntry.version == entry.version,
                            WaitlistEntry.status == "waiting",
                        )
                        .values(
                            status="offered",
                            offered_booking_id=offered_booking.id,
                            version=WaitlistEntry.version + 1,
                            updated_at=offer_now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if getattr(update_res, "rowcount", None) != 1:
                        raise RuntimeError(
                            f"Waitlist entry {entry.id} version moved during promotion attempt"
                        )

                    await append_event(
                        session,
                        event_type="waitlist_offered",
                        booking=offered_booking,
                        now=offer_now,
                    )
            except (IntegrityError, RuntimeError) as exc:
                if isinstance(exc, IntegrityError):
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
                else:
                    conflict = True

            if conflict:
                continue

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
    """Accept an offered waitlist entry (E16).

    Lock order: resource FOR UPDATE, then entry + booking FOR UPDATE (never booking first).
    Validates entry version pinned by If-Match before state checking (Spec 5.3).
    At or after exact deadline, persists expiration and promotion, then returns 409 HOLD_EXPIRED.
    """
    if scope.resource_id is None:
        raise RuntimeError("own_waitlist scope for accept is missing resource identity")

    # 1. Lock resource FOR UPDATE
    await session.execute(
        select(Resource.id).where(Resource.id == scope.resource_id).with_for_update()
    )

    # 2. Reselect waitlist entry FOR UPDATE
    waitlist_predicates = scope.predicates.get("waitlist")
    if not waitlist_predicates:
        raise RuntimeError("Owner-scoped waitlist operation requires dependency-supplied predicate")
    if not isinstance(waitlist_predicates, (list, tuple)):
        waitlist_predicates = (waitlist_predicates,)

    entry_stmt = (
        select(WaitlistEntry)
        .where(
            WaitlistEntry.id == entry_id,
            *waitlist_predicates,
        )
        .with_for_update()
    )
    entry = (await session.execute(entry_stmt)).scalar_one_or_none()
    if entry is None:
        raise NotFoundError("Waitlist entry not found")

    # 3. If-Match version check wins error precedence over state checking (Spec 5.3)
    if entry.version != expected_version:
        raise VersionMismatch("Waitlist entry has been modified by another request")

    # 4. State check
    if entry.status != "offered":
        raise InvalidState(f"Waitlist entry in status '{entry.status}' cannot be accepted")

    if entry.offered_booking_id is None:
        raise InvalidState("Offered waitlist entry has no linked offered booking")

    # 5. Reselect offered booking FOR UPDATE
    booking_stmt = select(Booking).where(Booking.id == entry.offered_booking_id).with_for_update()
    booking = (await session.execute(booking_stmt)).scalar_one_or_none()
    if booking is None or booking.status != "offered":
        raise InvalidState("Linked booking is not in offered status")

    # 6. Sample database clock for deadline evaluation
    db_clock = await session.scalar(select(func.clock_timestamp()))
    db_now = db_clock if db_clock is not None else now

    # At the exact deadline expiration wins (db_now >= expires_at)
    if booking.expires_at is not None and db_now >= booking.expires_at:
        # Persist expiration of entry and booking, promote waiters, commit, and return 409
        res_e = await session.execute(
            update(WaitlistEntry)
            .where(WaitlistEntry.id == entry.id, WaitlistEntry.version == entry.version)
            .values(
                status="expired",
                version=WaitlistEntry.version + 1,
                updated_at=db_now,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(res_e, "rowcount", None) != 1:
            raise RuntimeError(f"Waitlist entry {entry.id} version invariant violation")

        res_b = await session.execute(
            update(Booking)
            .where(Booking.id == booking.id, Booking.version == booking.version)
            .values(
                status="expired",
                expires_at=None,
                version=Booking.version + 1,
                updated_at=db_now,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(res_b, "rowcount", None) != 1:
            raise RuntimeError(f"Booking {booking.id} version invariant violation")
        await session.refresh(booking)

        await append_event(
            session,
            event_type="hold_expired",
            booking=booking,
            now=db_now,
        )

        await promote_waiters(session, scope.resource_id, db_now)

        return StoredResponse(
            status=409,
            body={
                "error": {
                    "code": "HOLD_EXPIRED",
                    "message": "The offer for this booking has expired",
                    "details": {},
                }
            },
            headers={},
        )

    # 7. Succeeded: transition booking to confirmed, entry to accepted
    res_b = await session.execute(
        update(Booking)
        .where(Booking.id == booking.id, Booking.version == booking.version)
        .values(
            status="confirmed",
            expires_at=None,
            version=Booking.version + 1,
            updated_at=db_now,
        )
        .execution_options(synchronize_session=False)
    )
    if getattr(res_b, "rowcount", None) != 1:
        raise RuntimeError(f"Booking {booking.id} version invariant violation")
    await session.refresh(booking)

    res_e = await session.execute(
        update(WaitlistEntry)
        .where(WaitlistEntry.id == entry.id, WaitlistEntry.version == entry.version)
        .values(
            status="accepted",
            version=WaitlistEntry.version + 1,
            updated_at=db_now,
        )
        .execution_options(synchronize_session=False)
    )
    if getattr(res_e, "rowcount", None) != 1:
        raise RuntimeError(f"Waitlist entry {entry.id} version invariant violation")

    await append_event(
        session,
        event_type="booking_confirmed",
        booking=booking,
        now=db_now,
    )

    booking_schema = BookingSchema.model_validate(booking)
    return StoredResponse(
        status=200,
        body=booking_schema.model_dump(mode="json"),
        headers={"ETag": f'"{booking.version}"'},
    )


async def decline_entry(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    entry_id: uuid.UUID,
    expected_version: int,
    now: datetime,
) -> WaitEntry:
    """Withdraw or decline a waitlist entry (E15).

    Holds resource FOR UPDATE, then entry FOR UPDATE.
    Validates entry version pinned by If-Match before state checking (Spec 5.3).
    A waiting entry withdrawal changes only the entry.
    An offered entry decline cancels both entry and booking, and promotes waiters.
    """
    if scope.resource_id is None:
        raise RuntimeError("own_waitlist scope for decline is missing resource identity")

    # 1. Lock resource FOR UPDATE
    await session.execute(
        select(Resource.id).where(Resource.id == scope.resource_id).with_for_update()
    )

    # 2. Reselect waitlist entry FOR UPDATE
    waitlist_predicates = scope.predicates.get("waitlist")
    if not waitlist_predicates:
        raise RuntimeError("Owner-scoped waitlist operation requires dependency-supplied predicate")
    if not isinstance(waitlist_predicates, (list, tuple)):
        waitlist_predicates = (waitlist_predicates,)

    entry_stmt = (
        select(WaitlistEntry)
        .where(
            WaitlistEntry.id == entry_id,
            *waitlist_predicates,
        )
        .with_for_update()
    )
    entry = (await session.execute(entry_stmt)).scalar_one_or_none()
    if entry is None:
        raise NotFoundError("Waitlist entry not found")

    # 3. If-Match check before state
    if entry.version != expected_version:
        raise VersionMismatch("Waitlist entry has been modified by another request")

    # Duplicate cancellation at current version returns 200 without new version
    if entry.status == "cancelled":
        return WaitEntry.model_validate(entry)

    if entry.status not in ("waiting", "offered"):
        raise InvalidState(f"Waitlist entry in status '{entry.status}' cannot be cancelled")

    db_clock = await session.scalar(select(func.clock_timestamp()))
    db_now = db_clock if db_clock is not None else now

    if db_now >= entry.time_range.lower:
        raise TooLate("A waitlist entry can only be withdrawn before its slot starts")

    if entry.status == "waiting":
        res_e = await session.execute(
            update(WaitlistEntry)
            .where(WaitlistEntry.id == entry.id, WaitlistEntry.version == entry.version)
            .values(
                status="cancelled",
                version=WaitlistEntry.version + 1,
                updated_at=db_now,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(res_e, "rowcount", None) != 1:
            raise RuntimeError(f"Waitlist entry {entry.id} version invariant violation")
        await session.refresh(entry)
        return WaitEntry.model_validate(entry)

    # entry.status == 'offered'
    if entry.offered_booking_id is not None:
        booking_stmt = (
            select(Booking).where(Booking.id == entry.offered_booking_id).with_for_update()
        )
        booking = (await session.execute(booking_stmt)).scalar_one_or_none()
        if booking is not None and booking.status == "offered":
            res_b = await session.execute(
                update(Booking)
                .where(Booking.id == booking.id, Booking.version == booking.version)
                .values(
                    status="cancelled",
                    expires_at=None,
                    version=Booking.version + 1,
                    updated_at=db_now,
                )
                .execution_options(synchronize_session=False)
            )
            if getattr(res_b, "rowcount", None) != 1:
                raise RuntimeError(f"Booking {booking.id} version invariant violation")

    res_e = await session.execute(
        update(WaitlistEntry)
        .where(WaitlistEntry.id == entry.id, WaitlistEntry.version == entry.version)
        .values(
            status="cancelled",
            version=WaitlistEntry.version + 1,
            updated_at=db_now,
        )
        .execution_options(synchronize_session=False)
    )
    if getattr(res_e, "rowcount", None) != 1:
        raise RuntimeError(f"Waitlist entry {entry.id} version invariant violation")
    await session.refresh(entry)

    await promote_waiters(session, scope.resource_id, db_now)

    return WaitEntry.model_validate(entry)
