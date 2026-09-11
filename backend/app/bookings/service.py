import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthorizedScope
from app.bookings.models import Booking
from app.bookings.schemas import Booking as BookingSchema
from app.bookings.schemas import BookingStatusFilter
from app.notifications.outbox import append_event
from app.resources.models import Resource
from app.resources.schemas import Page
from app.waitlist.models import WaitlistEntry


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


class VersionMismatch(Exception):
    """Raised when the version pinned by If-Match is no longer the stored version."""

    def __init__(self, message: str = "Object has been modified by another request") -> None:
        self.message = message
        super().__init__(message)


class TooLate(Exception):
    """Raised when a booking can no longer be cancelled by its owner."""

    def __init__(self, message: str = "A booking can only be cancelled before it starts") -> None:
        self.message = message
        super().__init__(message)


class InvalidState(Exception):
    """Raised when the stored state does not allow the requested transition."""

    def __init__(self, message: str = "Booking is in a state that cannot be cancelled") -> None:
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
            from app.observability.metrics import record_booking_conflict

            record_booking_conflict("create")
            raise SlotConflict("Slot conflict: requested time interval is not available") from exc
        raise

    return booking


async def _list_own_bookings(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    limit: int = 25,
    offset: int = 0,
    status: BookingStatusFilter | None = None,
) -> Page[BookingSchema]:
    """List the scope principal's own reservations, newest first.

    Blackouts are never reachable here: they carry no owner and are excluded by kind.
    """
    booking_predicates = scope.predicates.get("booking")
    if not booking_predicates:
        raise RuntimeError("Owner-scoped booking operation requires dependency-supplied predicate")
    if not isinstance(booking_predicates, (list, tuple)):
        booking_predicates = (booking_predicates,)

    conditions = [
        *booking_predicates,
        Booking.kind == "reservation",
    ]
    if status is not None:
        conditions.append(Booking.status == status.value)

    count_stmt = select(func.count()).select_from(Booking).where(*conditions)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Booking)
        .where(*conditions)
        .order_by(Booking.created_at.desc(), Booking.id.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()

    return Page[BookingSchema](
        items=[BookingSchema.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _get_own_booking(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
) -> BookingSchema:
    """Read the reservation already resolved as owned by the scope principal."""
    booking_predicates = scope.predicates.get("booking")
    if not booking_predicates:
        raise RuntimeError("Owner-scoped booking operation requires dependency-supplied predicate")
    if not isinstance(booking_predicates, (list, tuple)):
        booking_predicates = (booking_predicates,)

    stmt = select(Booking).where(
        Booking.id == scope.object_id,
        Booking.kind == "reservation",
        *booking_predicates,
    )
    booking = (await session.execute(stmt)).scalar_one_or_none()
    if booking is None:
        raise NotFoundError("Booking not found")
    return BookingSchema.model_validate(booking)


async def cancel_booking(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    booking_id: uuid.UUID,
    expected_version: int,
    reason: str,
    now: datetime,
) -> BookingSchema:
    """Cancel the authorized booking and append its cancellation event atomically.

    The resource row is locked FOR UPDATE before the booking row is re-selected, so a
    release is exclusive against competing creates on the same resource. A stale
    expected version is reported before any state or time check. Cancelling an already
    cancelled booking at its current version is a no-op that returns the stored booking
    without a new version or a second event.

    The caller supplies an open transaction; deadline comparisons use the database clock
    sampled once both rows are held, rather than `now`, which only dates the request.
    """
    if scope.resource_id is None:
        raise NotFoundError("Booking not found")

    lock_stmt = select(Resource.id).where(Resource.id == scope.resource_id).with_for_update()
    await session.execute(lock_stmt)

    booking_predicates = scope.predicates.get("booking")
    if booking_predicates is None:
        raise RuntimeError("Owner-scoped booking operation requires dependency-supplied predicate")
    if not isinstance(booking_predicates, (list, tuple)):
        booking_predicates = (booking_predicates,)

    booking_stmt = (
        select(Booking)
        .where(
            Booking.id == booking_id,
            Booking.kind == "reservation",
            *booking_predicates,
        )
        .with_for_update()
    )
    booking = (await session.execute(booking_stmt)).scalar_one_or_none()
    if booking is None:
        raise NotFoundError("Booking not found")

    # Sample the database clock only once the booking row is held. Acquiring the two
    # locks above can block for as long as `lock_timeout`, and a booking that started
    # during that wait is no longer cancellable by its owner (Spec 1.2, 5.3).
    db_clock = await session.scalar(select(func.clock_timestamp()))
    db_now = db_clock if db_clock is not None else now

    if booking.version != expected_version:
        raise VersionMismatch("Booking has been modified by another request")

    if booking.status == "cancelled":
        return BookingSchema.model_validate(booking)

    if booking.status not in ("confirmed", "offered"):
        raise InvalidState(f"A booking in state '{booking.status}' cannot be cancelled")

    if scope.predicates.get("allow_running", False):
        if db_now >= booking.time_range.upper:
            raise TooLate("A completed booking cannot be cancelled")
    else:
        if db_now >= booking.time_range.lower:
            raise TooLate("A booking can only be cancelled before it starts")

    update_stmt = (
        update(Booking)
        .where(Booking.id == booking_id, Booking.version == expected_version)
        .values(
            status="cancelled",
            expires_at=None,
            cancellation_reason=reason,
            version=Booking.version + 1,
            updated_at=db_now,
        )
        .execution_options(synchronize_session=False)
    )
    # The row is held FOR UPDATE and its version was verified above, so the conditional
    # WHERE always matches here; it is kept because it is the correct optimistic-
    # concurrency idiom and it keeps the update safe if the guard above ever moves.
    res_b = await session.execute(update_stmt)
    if getattr(res_b, "rowcount", None) != 1:
        raise RuntimeError(f"Booking {booking_id} version invariant violation")

    # Re-read the row the update just wrote, so the response and the outbox payload carry
    # the persisted values rather than the stale identity-map copy.
    await session.refresh(booking)

    # Update linked offered entry if any (Spec 5.3)
    entry_stmt = (
        update(WaitlistEntry)
        .where(
            WaitlistEntry.offered_booking_id == booking_id,
            WaitlistEntry.status == "offered",
        )
        .values(
            status="cancelled",
            version=WaitlistEntry.version + 1,
            updated_at=db_now,
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(entry_stmt)

    await append_event(
        session,
        event_type="booking_cancelled",
        booking=booking,
        now=db_now,
    )

    if scope.predicates.get("audit"):
        from app.admin.audit import append_audit_log

        req_id = scope.predicates.get("request_id") or uuid.uuid4()
        await append_audit_log(
            session,
            action="admin.booking_cancel",
            target_type="booking",
            target_id=booking_id,
            actor_id=scope.principal_id,
            request_id=req_id,
            details={
                "resource_id": str(scope.resource_id),
                "reason_length": len(reason),
            },
            now=db_now,
        )

    from app.waitlist.service import promote_waiters

    await promote_waiters(session, scope.resource_id, db_now)

    return BookingSchema.model_validate(booking)
