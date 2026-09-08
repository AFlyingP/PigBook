"""Hold expiry and promotion scheduler.

Fixed public interface (Spec 3.4):
    expire_and_promote(session, *, resource_id: UUID, now: datetime) -> int

expire_and_promote return value: the number of offered bookings expired by
that call (0 when the call only performed promotion repair or skipped a locked
resource).

This module discovers resources with overdue offered bookings or waiting
entries, acquires resource row locks with FOR UPDATE SKIP LOCKED, transitions
expired holds to terminal status with version increments and outbox events, and
calls promote_waiters to advance queues.
"""

import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bookings.models import Booking
from app.notifications.models import WorkerHeartbeat
from app.notifications.outbox import append_event
from app.resources.models import Resource
from app.waitlist.models import WaitlistEntry
from app.waitlist.service import promote_waiters


async def expire_and_promote(
    session: AsyncSession,
    *,
    resource_id: uuid.UUID,
    now: datetime,
    skip_locked: bool = True,
) -> int:
    """Expire overdue offered bookings on a resource and promote waiting entries.

    Fixed public interface (Spec 3.4).
    Accepts an already-open transaction and never commits it.

    Acquires the resource row FOR UPDATE (with SKIP LOCKED by default).
    If the resource cannot be locked or does not exist, returns 0.
    Samples clock_timestamp() only after acquiring the resource lock.
    Transitions due offered bookings (status='offered' AND expires_at <= db_now)
    and their linked waitlist entries to 'expired' with atomic version increments,
    clears expires_at, appends hold_expired outbox events, and calls promote_waiters.

    Returns the number of offered bookings expired by that call (0 when the call
    only performed promotion repair or skipped a locked resource).
    """
    # 1. Acquire resource row lock
    stmt = (
        select(Resource).where(Resource.id == resource_id).with_for_update(skip_locked=skip_locked)
    )
    resource = (await session.execute(stmt)).scalar_one_or_none()
    if resource is None:
        return 0

    # 2. Sample clock_timestamp() authoritatively AFTER acquiring resource lock
    db_clock = await session.scalar(select(func.clock_timestamp()))
    db_now = db_clock if db_clock is not None else now

    # 3. Lock and fetch due offered bookings
    stmt_bookings = (
        select(Booking)
        .where(
            Booking.resource_id == resource_id,
            Booking.status == "offered",
            Booking.expires_at <= db_now,
        )
        .with_for_update()
    )
    due_bookings: Sequence[Booking] = (await session.execute(stmt_bookings)).scalars().all()

    expired_count = 0
    for booking in due_bookings:
        # Lock linked waitlist entry if present
        stmt_entry = (
            select(WaitlistEntry)
            .where(WaitlistEntry.offered_booking_id == booking.id)
            .with_for_update()
        )
        entry = (await session.execute(stmt_entry)).scalar_one_or_none()

        if entry is not None:
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

        # Transition booking to expired, clear expires_at, increment version
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

        # Append exactly one hold_expired outbox event
        await append_event(
            session,
            event_type="hold_expired",
            booking=booking,
            now=db_now,
        )
        expired_count += 1

    # 4. Repair free capacity by promoting eligible waiters
    await promote_waiters(session, resource_id, db_now)

    return expired_count


async def discover_candidate_resources(
    session: AsyncSession,
    *,
    cursor: uuid.UUID | None,
    limit: int = 100,
    resource_ids: Sequence[uuid.UUID] | None = None,
) -> tuple[list[uuid.UUID], bool]:
    """Discover candidate resource IDs that have overdue holds or waiting entries.

    Returns (resource_ids, reached_end).
    When reaching the end of the keyset, reached_end is True.
    """

    async def _query(after_cursor: uuid.UUID | None) -> list[uuid.UUID]:
        q_bookings = select(Booking.resource_id).where(
            Booking.status == "offered",
            Booking.expires_at <= func.clock_timestamp(),
        )
        q_waitlist = select(WaitlistEntry.resource_id).where(WaitlistEntry.status == "waiting")
        union_subq = q_bookings.union(q_waitlist).subquery("candidates")
        stmt = (
            select(union_subq.c.resource_id).order_by(union_subq.c.resource_id.asc()).limit(limit)
        )
        if resource_ids is not None:
            stmt = stmt.where(union_subq.c.resource_id.in_(resource_ids))
        if after_cursor is not None:
            stmt = stmt.where(union_subq.c.resource_id > after_cursor)
        return list((await session.execute(stmt)).scalars().all())

    candidates = await _query(cursor)
    if not candidates and cursor is not None:
        # Wrap to beginning
        candidates = await _query(None)
        reached_end = len(candidates) < limit
        return candidates, reached_end

    reached_end = len(candidates) < limit
    return candidates, reached_end


async def record_heartbeat(
    session: AsyncSession,
    *,
    seen: bool = True,
    expiry_scan: bool = False,
    now: datetime | None = None,
) -> None:
    """Update worker heartbeat row with seen_at and/or expiry_scan_at."""
    db_now = now or func.clock_timestamp()
    values = {"name": "primary", "seen_at": db_now, "expiry_scan_at": db_now}
    set_dict: dict[str, object] = {}
    if seen:
        set_dict["seen_at"] = db_now
    if expiry_scan:
        set_dict["expiry_scan_at"] = db_now

    stmt = (
        insert(WorkerHeartbeat)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["name"],
            set_=set_dict,
        )
    )
    await session.execute(stmt)


class HoldExpiryScheduler:
    """In-memory stateful scheduler discovering and processing resources."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        batch_size: int = 100,
        resource_ids: Sequence[uuid.UUID] | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.batch_size = batch_size
        self.resource_ids = resource_ids
        self.cursor: uuid.UUID | None = None

    async def run_cycle(self, now: datetime | None = None) -> int:
        """Run one discovery and expiry cycle across candidate resources.

        Advances the in-memory UUID cursor past every examined resource,
        including resources skipped due to unavailable locks, and wraps
        to the beginning after reaching the end.
        """
        async with self.sessionmaker() as session:
            candidates, reached_end = await discover_candidate_resources(
                session,
                cursor=self.cursor,
                limit=self.batch_size,
                resource_ids=self.resource_ids,
            )

        total_expired = 0
        ref_now = now or datetime.now(timezone.utc)
        for resource_id in candidates:
            self.cursor = resource_id
            async with self.sessionmaker() as session:
                async with session.begin():
                    expired = await expire_and_promote(
                        session,
                        resource_id=resource_id,
                        now=ref_now,
                        skip_locked=True,
                    )
                    total_expired += expired

        if reached_end:
            self.cursor = None

        # Update expiry_scan_at on worker_heartbeat after complete discovery batch
        async with self.sessionmaker() as session:
            async with session.begin():
                await record_heartbeat(session, seen=True, expiry_scan=True, now=now)

        return total_expired
