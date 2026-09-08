"""Integration tests for waitlist promotion (Phase B / legacy T-015)."""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.auth.models import User
from app.auth.passwords import hash_password
from app.bookings.models import Booking
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app
from app.notifications.models import Outbox
from app.resources.models import Resource
from app.waitlist.models import WaitlistEntry
from app.waitlist.service import promote_waiters

get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _cleanup_engine() -> Any:
    yield
    import app.db.session

    if app.db.session._engine is not None:
        await app.db.session._engine.dispose()
        app.db.session._engine = None
        app.db.session._sessionmaker = None
        app.db.session._engine_url = None


def make_client(ip: str = "127.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False, client=(ip, 12345)),
        base_url="http://localhost:5173",
    )


def make_token(user: User) -> str:
    settings = get_settings()
    jwt_secret = settings.JWT_SECRET or "test-jwt-secret-minimum-32-bytes-long-12345678"
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user.id),
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "jti": str(uuid.uuid4()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
    }
    return jwt.encode(claims, jwt_secret, algorithm="HS256")


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(user)}"}


async def create_user(*, role: str = "member", enabled: bool = True) -> User:
    email = f"prom_{uuid.uuid4().hex[:8]}@example.com"
    pwd_hash = hash_password("valid-password-123")
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=pwd_hash,
        display_name="Promotion User",
        role=role,
        enabled=enabled,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(user)
    return user


async def create_resource(*, active: bool = True) -> Resource:
    resource = Resource(
        id=uuid.uuid4(),
        name=f"PromResource_{uuid.uuid4().hex[:6]}",
        description="Promotion Test Resource",
        location="Studio B",
        active=active,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(resource)
    return resource


async def insert_booking(
    *,
    resource: Resource,
    owner: User | None,
    created_by: User,
    starts_at: datetime,
    ends_at: datetime,
    status: str = "confirmed",
    kind: str = "reservation",
) -> Booking:
    booking = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=owner.id if owner is not None else None,
        created_by=created_by.id,
        kind=kind,
        time_range=Range(starts_at, ends_at, bounds="[)"),
        status=status,
        expires_at=None,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(booking)
    return booking


async def insert_waitlist_entry(
    *,
    resource: Resource,
    user: User,
    starts_at: datetime,
    ends_at: datetime,
    status: str = "waiting",
    created_at: datetime | None = None,
) -> WaitlistEntry:
    now = datetime.now(timezone.utc)
    entry = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=user.id,
        resource_id=resource.id,
        time_range=Range(starts_at, ends_at, bounds="[)"),
        status=status,
        offered_booking_id=None,
        version=1,
        created_at=created_at or now,
        updated_at=created_at or now,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(entry)
    return entry


def aligned_slot(
    days: int = 5, hour: int = 10, duration_hours: int = 1
) -> tuple[datetime, datetime]:
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(
        days=days
    )
    start = base.replace(hour=hour)
    end = start + timedelta(hours=duration_hours)
    return start, end


# --- Tests ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlapping_vs_disjoint_windows() -> None:
    """Blocked older overlapping window does not block a younger disjoint free window."""
    resource = await create_resource()
    creator = await create_user()
    user_blocked = await create_user()
    user_disjoint = await create_user()

    # Slot 1: [10:00, 11:00] occupied
    s1, e1 = aligned_slot(days=5, hour=10)
    await insert_booking(
        resource=resource,
        owner=creator,
        created_by=creator,
        starts_at=s1,
        ends_at=e1,
    )

    t0 = datetime.now(timezone.utc)
    # Older entry blocked by Slot 1: [10:30, 11:30]
    entry_blocked = await insert_waitlist_entry(
        resource=resource,
        user=user_blocked,
        starts_at=s1 + timedelta(minutes=30),
        ends_at=e1 + timedelta(minutes=30),
        created_at=t0,
    )

    # Younger entry for disjoint free slot: [14:00, 15:00]
    s2, e2 = aligned_slot(days=5, hour=14)
    entry_disjoint = await insert_waitlist_entry(
        resource=resource,
        user=user_disjoint,
        starts_at=s2,
        ends_at=e2,
        created_at=t0 + timedelta(minutes=1),
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            promoted = await promote_waiters(session, resource.id, datetime.now(timezone.utc))

    assert len(promoted) == 1

    # Verify blocked entry is still waiting
    async with sessionmaker() as session:
        b_entry = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_blocked.id))
        ).scalar_one()
        assert b_entry.status == "waiting"
        assert b_entry.offered_booking_id is None

        # Verify disjoint entry is offered
        d_entry = (
            await session.execute(
                select(WaitlistEntry).where(WaitlistEntry.id == entry_disjoint.id)
            )
        ).scalar_one()
        assert d_entry.status == "offered"
        assert d_entry.offered_booking_id == promoted[0]


@pytest.mark.asyncio
async def test_disabled_owner_and_too_late_expire() -> None:
    """Disabled owner and too-late (starts_at < now + 15m) entries expire and receive no offers."""
    resource = await create_resource()
    disabled_user = await create_user(enabled=False)
    late_user = await create_user(enabled=True)

    # Free slots far in future for disabled, near in future for late
    s_far, e_far = aligned_slot(days=5, hour=10)
    entry_disabled = await insert_waitlist_entry(
        resource=resource,
        user=disabled_user,
        starts_at=s_far,
        ends_at=e_far,
    )

    now = datetime.now(timezone.utc)
    s_near = now + timedelta(minutes=10)  # less than 15 minutes ahead
    e_near = s_near + timedelta(hours=1)
    entry_late = await insert_waitlist_entry(
        resource=resource,
        user=late_user,
        starts_at=s_near,
        ends_at=e_near,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            promoted = await promote_waiters(session, resource.id, now)

    assert len(promoted) == 0

    async with sessionmaker() as session:
        d_row = (
            await session.execute(
                select(WaitlistEntry).where(WaitlistEntry.id == entry_disabled.id)
            )
        ).scalar_one()
        assert d_row.status == "expired"
        assert d_row.version == 2

        l_row = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_late.id))
        ).scalar_one()
        assert l_row.status == "expired"
        assert l_row.version == 2


@pytest.mark.asyncio
async def test_offered_occupancy_excludes_further_offers() -> None:
    """An offered booking occupies inventory and excludes subsequent offers for the

    same interval.
    """
    resource = await create_resource()
    user_1 = await create_user()
    user_2 = await create_user()
    s, e = aligned_slot(days=6)

    t0 = datetime.now(timezone.utc)
    # Older waiter
    e1 = await insert_waitlist_entry(
        resource=resource,
        user=user_1,
        starts_at=s,
        ends_at=e,
        created_at=t0,
    )
    # Younger waiter for the same window
    e2 = await insert_waitlist_entry(
        resource=resource,
        user=user_2,
        starts_at=s,
        ends_at=e,
        created_at=t0 + timedelta(seconds=5),
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            promoted = await promote_waiters(session, resource.id, t0)

    # Exactly one offer created (for user_1)
    assert len(promoted) == 1

    async with sessionmaker() as session:
        row1 = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == e1.id))
        ).scalar_one()
        assert row1.status == "offered"

        row2 = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == e2.id))
        ).scalar_one()
        assert row2.status == "waiting"


@pytest.mark.asyncio
async def test_cancellation_competing_with_create_no_gap() -> None:
    """Cancellation promotes eligible waiter atomically so fresh create cannot take the slot."""
    resource = await create_resource()
    owner = await create_user()
    waiter = await create_user()
    competitor = await create_user()
    s, e = aligned_slot(days=7)

    booking = await insert_booking(
        resource=resource,
        owner=owner,
        created_by=owner,
        starts_at=s,
        ends_at=e,
        status="confirmed",
    )

    waiter_entry = await insert_waitlist_entry(
        resource=resource,
        user=waiter,
        starts_at=s,
        ends_at=e,
    )

    async with make_client() as client:
        # Owner cancels reservation
        cancel_res = await client.post(
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": "cancelled by owner"},
            headers={**auth(owner), "If-Match": '"1"'},
        )
        assert cancel_res.status_code == 200

        # Competitor immediately attempts to book the same slot: rejected with 409 SLOT_CONFLICT
        # because the offered booking was already committed during cancellation!
        create_res = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": str(resource.id),
                "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            headers={
                **auth(competitor),
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        assert create_res.status_code == 409
        assert create_res.json()["error"]["code"] == "SLOT_CONFLICT"

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        entry_row = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == waiter_entry.id))
        ).scalar_one()
        assert entry_row.status == "offered"
        assert entry_row.offered_booking_id is not None


@pytest.mark.asyncio
async def test_keyset_scan_100_blocked_then_101st_eligible_and_fifo_order() -> None:
    """100 older blocked waiting entries followed by an eligible 101st entry receives an offer."""
    resource = await create_resource()
    creator = await create_user()

    pwd_hash = hash_password("valid-password-123")
    dummy_users = [
        User(
            id=uuid.uuid4(),
            email=f"u_{i}_{uuid.uuid4().hex[:6]}@example.com",
            password_hash=pwd_hash,
            display_name=f"User {i}",
            role="member",
            enabled=True,
            version=1,
        )
        for i in range(100)
    ]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add_all(dummy_users)

    # Slot 1: [10:00, 11:00] occupied
    s1, e1 = aligned_slot(days=8, hour=10)
    await insert_booking(
        resource=resource,
        owner=creator,
        created_by=creator,
        starts_at=s1,
        ends_at=e1,
    )

    base_time = datetime.now(timezone.utc) - timedelta(hours=2)
    blocked_entries = []
    # 100 entries on Slot 1 (all blocked, each from distinct user)
    for i in range(100):
        blocked_entries.append(
            WaitlistEntry(
                id=uuid.uuid4(),
                user_id=dummy_users[i].id,
                resource_id=resource.id,
                time_range=Range(s1, e1, bounds="[)"),
                status="waiting",
                version=1,
                created_at=base_time + timedelta(seconds=i),
                updated_at=base_time + timedelta(seconds=i),
            )
        )

    # 101st entry on a free disjoint slot
    s_free, e_free = aligned_slot(days=8, hour=15)
    eligible_user = await create_user()
    entry_101 = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=eligible_user.id,
        resource_id=resource.id,
        time_range=Range(s_free, e_free, bounds="[)"),
        status="waiting",
        version=1,
        created_at=base_time + timedelta(seconds=101),
        updated_at=base_time + timedelta(seconds=101),
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add_all(blocked_entries)
            session.add(entry_101)

    async with sessionmaker() as session:
        async with session.begin():
            promoted = await promote_waiters(session, resource.id, datetime.now(timezone.utc))

    assert len(promoted) == 1

    async with sessionmaker() as session:
        row_101 = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_101.id))
        ).scalar_one()
        assert row_101.status == "offered"
        assert row_101.offered_booking_id == promoted[0]


@pytest.mark.asyncio
async def test_expires_at_short_lead_and_event_correctness() -> None:
    """expires_at <= starts_at for short-lead window; waitlist_offered event

    count/version correct.
    """
    resource = await create_resource()
    user = await create_user()

    # Lead time is exactly 20 minutes from now (between 15m and 30m)
    now = datetime.now(timezone.utc)
    s = now + timedelta(minutes=20)
    e = s + timedelta(hours=1)

    await insert_waitlist_entry(
        resource=resource,
        user=user,
        starts_at=s,
        ends_at=e,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            promoted = await promote_waiters(session, resource.id, now)

    assert len(promoted) == 1
    booking_id = promoted[0]

    async with sessionmaker() as session:
        booking = (
            await session.execute(select(Booking).where(Booking.id == booking_id))
        ).scalar_one()
        assert booking.status == "offered"
        assert booking.expires_at is not None
        assert booking.expires_at <= booking.time_range.lower
        assert booking.version == 1

        # Check outbox event
        outbox_events = (
            (
                await session.execute(
                    select(Outbox).where(
                        Outbox.aggregate_id == booking_id,
                        Outbox.event_type == "waitlist_offered",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(outbox_events) == 1
        event = outbox_events[0]
        assert event.aggregate_version == 1
        assert event.payload["schema_version"] == 1
        assert event.payload["booking_id"] == str(booking_id)
        assert event.payload["recipient_id"] == str(user.id)
        assert event.payload["resource_id"] == str(resource.id)


@pytest.mark.asyncio
async def test_direct_writer_exclusion_conflict_savepoint_synthetic() -> None:
    """Synthetic branch test: direct-writer exclusion conflict during promotion

    leaves entry waiting.
    """
    resource = await create_resource()
    user_1 = await create_user()
    user_2 = await create_user()

    s1, e1 = aligned_slot(days=9, hour=10)
    s2, e2 = aligned_slot(days=9, hour=14)

    t0 = datetime.now(timezone.utc)
    entry_1 = await insert_waitlist_entry(
        resource=resource,
        user=user_1,
        starts_at=s1,
        ends_at=e1,
        created_at=t0,
    )
    entry_2 = await insert_waitlist_entry(
        resource=resource,
        user=user_2,
        starts_at=s2,
        ends_at=e2,
        created_at=t0 + timedelta(seconds=1),
    )

    sessionmaker = get_sessionmaker()

    # Simulate a 23P01 exclusion conflict on the first offer insertion
    # by inserting a conflicting booking directly right before the savepoint
    # or by raising an IntegrityError with pgcode 23P01
    class FakeOrig:
        sqlstate = "23P01"
        pgcode = "23P01"
        constraint_name = "bookings_no_overlap"

    conflict_exc = IntegrityError("exclusion conflict", params={}, orig=FakeOrig())
    call_count = 0

    async with sessionmaker() as session:
        async with session.begin():
            real_flush = session.flush

            async def patched_flush(*args: Any, **kwargs: Any) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise conflict_exc
                return await real_flush(*args, **kwargs)

            session.flush = patched_flush  # type: ignore[assignment]
            promoted = await promote_waiters(session, resource.id, datetime.now(timezone.utc))

    # Entry 1 should have had its savepoint rolled back and remained waiting;
    # Entry 2 should have been successfully promoted!
    assert len(promoted) == 1

    async with sessionmaker() as session:
        row_1 = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_1.id))
        ).scalar_one()
        assert row_1.status == "waiting"

        row_2 = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_2.id))
        ).scalar_one()
        assert row_2.status == "offered"
        assert row_2.offered_booking_id == promoted[0]


@pytest.mark.asyncio
async def test_direct_writer_exclusion_conflict_real() -> None:
    """A real PostgreSQL 23P01 exclusion conflict from an uncommitted writer

    rolls back that offer savepoint, leaving the entry waiting, while a subsequent
    disjoint entry is promoted (Spec 5.3, R14).
    """
    resource = await create_resource()
    user_1 = await create_user()
    user_2 = await create_user()
    direct_writer = await create_user()

    s1, e1 = aligned_slot(days=17, hour=10)
    s2, e2 = aligned_slot(days=17, hour=14)

    t0 = datetime.now(timezone.utc)
    entry_1 = await insert_waitlist_entry(
        resource=resource,
        user=user_1,
        starts_at=s1,
        ends_at=e1,
        created_at=t0,
    )
    entry_2 = await insert_waitlist_entry(
        resource=resource,
        user=user_2,
        starts_at=s2,
        ends_at=e2,
        created_at=t0 + timedelta(seconds=1),
    )

    sessionmaker = get_sessionmaker()

    # Session 1: Direct database writer (bypassing service and resource lock)
    # inserts an overlapping booking and flushes (holding row lock in GiST index)
    # but does NOT commit yet.
    direct_booking = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=direct_writer.id,
        created_by=direct_writer.id,
        kind="reservation",
        time_range=Range(s1, e1, bounds="[)"),
        status="confirmed",
        expires_at=None,
        version=1,
    )

    async with sessionmaker() as writer_session:
        async with writer_session.begin():
            writer_session.add(direct_booking)
            await writer_session.flush()

            # Session 2: run promote_waiters in an asyncio task.
            # promote_waiters reads READ COMMITTED (does not see uncommitted direct_booking),
            # attempts to insert offer for entry_1, and blocks on PostgreSQL GiST index!
            async def run_promoter() -> list[uuid.UUID]:
                async with sessionmaker() as promoter_session:
                    async with promoter_session.begin():
                        return await promote_waiters(
                            promoter_session, resource.id, datetime.now(timezone.utc)
                        )

            promoter_task = asyncio.create_task(run_promoter())
            await asyncio.sleep(0.5)

        # writer_session commits here as context manager exits!
        # Unblocks promoter_task with genuine PostgreSQL 23P01 ExclusionViolationError!
        promoted = await promoter_task

    assert len(promoted) == 1

    async with sessionmaker() as session:
        # Entry 1 remained waiting
        row_1 = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_1.id))
        ).scalar_one()
        assert row_1.status == "waiting"
        assert row_1.offered_booking_id is None

        # Entry 2 was successfully promoted
        row_2 = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_2.id))
        ).scalar_one()
        assert row_2.status == "offered"
        assert row_2.offered_booking_id == promoted[0]


@pytest.mark.asyncio
async def test_promotion_entry_version_conflict_aborts_offer_without_orphan_booking() -> None:
    """If an entry's version moves during promotion, the offer attempt rolls back

    atomically without committing an orphan offered booking (Spec 5.3, R10).
    """
    resource = await create_resource()
    user_1 = await create_user()
    user_2 = await create_user()

    s1, e1 = aligned_slot(days=18, hour=10)
    s2, e2 = aligned_slot(days=18, hour=14)

    t0 = datetime.now(timezone.utc)
    entry_1 = await insert_waitlist_entry(
        resource=resource,
        user=user_1,
        starts_at=s1,
        ends_at=e1,
        created_at=t0,
    )
    entry_2 = await insert_waitlist_entry(
        resource=resource,
        user=user_2,
        starts_at=s2,
        ends_at=e2,
        created_at=t0 + timedelta(seconds=1),
    )

    sessionmaker = get_sessionmaker()

    # Hook flush to move entry_1's version in an independent session
    # immediately after the offered booking is staged inside the savepoint,
    # before the update(WaitlistEntry) statement runs.
    async with sessionmaker() as session:
        async with session.begin():
            real_flush = session.flush
            entry_1_staged = False

            async def hooked_flush(*args: Any, **kwargs: Any) -> None:
                nonlocal entry_1_staged
                await real_flush(*args, **kwargs)
                if not entry_1_staged:
                    entry_1_staged = True
                    async with sessionmaker() as bg_session:
                        async with bg_session.begin():
                            await bg_session.execute(
                                update(WaitlistEntry)
                                .where(WaitlistEntry.id == entry_1.id)
                                .values(version=99)
                            )

            session.flush = hooked_flush  # type: ignore[assignment]
            promoted = await promote_waiters(session, resource.id, datetime.now(timezone.utc))

    # Entry 1 offer aborted due to version conflict; Entry 2 promoted cleanly
    assert len(promoted) == 1

    async with sessionmaker() as session:
        # Entry 1 remains waiting with version 99, no offered booking linked
        row_1 = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_1.id))
        ).scalar_one()
        assert row_1.status == "waiting"
        assert row_1.version == 99
        assert row_1.offered_booking_id is None

        # Assert no orphan booking exists for user_1 on slot 1
        orphan_bookings = (
            (
                await session.execute(
                    select(Booking).where(
                        Booking.resource_id == resource.id,
                        Booking.user_id == user_1.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(orphan_bookings) == 0

        # Assert no outbox events exist for user_1
        events_1 = (
            (
                await session.execute(
                    select(Outbox).where(Outbox.payload["recipient_id"].astext == str(user_1.id))
                )
            )
            .scalars()
            .all()
        )
        assert len(events_1) == 0

        # Entry 2 was promoted
        row_2 = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_2.id))
        ).scalar_one()
        assert row_2.status == "offered"
        assert row_2.offered_booking_id == promoted[0]
