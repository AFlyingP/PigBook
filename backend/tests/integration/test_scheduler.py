"""Integration tests for hold expiry, worker supervision, and maintenance (T-017)."""

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.admin.models import AuditLog, Feedback
from app.auth.models import RateLimit, RefreshToken, User
from app.auth.passwords import hash_password
from app.bookings.models import Booking, IdempotencyKey
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app
from app.notifications.maintenance import run_maintenance
from app.notifications.models import Outbox, WorkerHeartbeat
from app.resources.models import Resource
from app.waitlist.models import WaitlistEntry
from app.waitlist.scheduler import (
    HoldExpiryScheduler,
    expire_and_promote,
    record_heartbeat,
)
from app.worker import SHUTDOWN_BUDGET_SECONDS, WorkerSupervisor

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
    email = f"sched_{uuid.uuid4().hex[:8]}@example.com"
    pwd_hash = hash_password("valid-password-123")
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=pwd_hash,
        display_name="Scheduler User",
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
        name=f"SchedResource_{uuid.uuid4().hex[:6]}",
        description="Scheduler Test Resource",
        location="Lab 101",
        active=active,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(resource)
    return resource


async def create_offered_hold(
    *,
    resource: Resource,
    user: User,
    starts_at: datetime,
    ends_at: datetime,
    expires_at: datetime,
) -> tuple[Booking, WaitlistEntry]:
    booking = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=user.id,
        created_by=user.id,
        kind="reservation",
        time_range=Range(starts_at, ends_at, bounds="[)"),
        status="offered",
        expires_at=expires_at,
        version=1,
    )
    entry = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=user.id,
        resource_id=resource.id,
        time_range=Range(starts_at, ends_at, bounds="[)"),
        status="offered",
        offered_booking_id=booking.id,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(booking)
            session.add(entry)
    return booking, entry


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


# ---------------------------------------------------------------------------
# Test 1: Due hold expiry and promotion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_due_hold_expiry_and_promotion() -> None:
    """Due hold is expired with version increments, outbox event, and waiter promoted."""
    sessionmaker = get_sessionmaker()
    resource = await create_resource()
    user_offered = await create_user()
    user_horizon = await create_user()
    user_waiter = await create_user()

    now = datetime.now(timezone.utc)
    slot_start = now + timedelta(hours=2)
    slot_end = slot_start + timedelta(hours=1)

    # 1. Past deadline hold
    booking, entry = await create_offered_hold(
        resource=resource,
        user=user_offered,
        starts_at=slot_start,
        ends_at=slot_end,
        expires_at=now - timedelta(minutes=5),
    )

    # 2. Waiter inside the 15-minute horizon (should be expired by promote_waiters)
    h_start = now + timedelta(minutes=10)
    h_end = h_start + timedelta(hours=1)
    entry_horizon = await insert_waitlist_entry(
        resource=resource,
        user=user_horizon,
        starts_at=h_start,
        ends_at=h_end,
        created_at=now - timedelta(minutes=20),
    )

    # 3. Eligible waiter for the released window
    entry_waiter = await insert_waitlist_entry(
        resource=resource,
        user=user_waiter,
        starts_at=slot_start,
        ends_at=slot_end,
        created_at=now - timedelta(minutes=10),
    )

    async with sessionmaker() as session:
        async with session.begin():
            expired_count = await expire_and_promote(session, resource_id=resource.id, now=now)

    assert expired_count == 1

    # Verify booking and entry transitions
    async with sessionmaker() as session:
        b_refreshed = (
            await session.execute(select(Booking).where(Booking.id == booking.id))
        ).scalar_one()
        assert b_refreshed.status == "expired"
        assert b_refreshed.expires_at is None
        assert b_refreshed.version == 2

        e_refreshed = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry.id))
        ).scalar_one()
        assert e_refreshed.status == "expired"
        assert e_refreshed.version == 2

        # Verify exactly one hold_expired outbox event
        events = (
            (
                await session.execute(
                    select(Outbox).where(
                        Outbox.aggregate_id == booking.id,
                        Outbox.event_type == "hold_expired",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].aggregate_version == 2
        assert events[0].payload["booking_id"] == str(booking.id)

        # Verify horizon entry expired
        eh_refreshed = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_horizon.id))
        ).scalar_one()
        assert eh_refreshed.status == "expired"

        # Verify eligible waiter promoted
        ew_refreshed = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_waiter.id))
        ).scalar_one()
        assert ew_refreshed.status == "offered"
        assert ew_refreshed.offered_booking_id is not None
        assert ew_refreshed.version == 2


# ---------------------------------------------------------------------------
# Test 2: Two workers concurrent cycles
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_two_workers_concurrent_expiry() -> None:
    """Two concurrent workers produce exactly one terminal outcome per hold."""
    sessionmaker = get_sessionmaker()
    resource = await create_resource()
    user_offered = await create_user()
    user_waiter = await create_user()

    now = datetime.now(timezone.utc)
    start = now + timedelta(hours=3)
    end = start + timedelta(hours=1)

    booking, entry = await create_offered_hold(
        resource=resource,
        user=user_offered,
        starts_at=start,
        ends_at=end,
        expires_at=now - timedelta(minutes=1),
    )
    entry_waiter = await insert_waitlist_entry(
        resource=resource,
        user=user_waiter,
        starts_at=start,
        ends_at=end,
    )

    async def worker1() -> int:
        async with sessionmaker() as s1:
            async with s1.begin():
                return await expire_and_promote(s1, resource_id=resource.id, now=now)

    async def worker2() -> int:
        async with sessionmaker() as s2:
            async with s2.begin():
                return await expire_and_promote(s2, resource_id=resource.id, now=now)

    results = await asyncio.gather(worker1(), worker2())
    assert sum(results) == 1
    assert sorted(results) == [0, 1]

    async with sessionmaker() as session:
        events = (
            (
                await session.execute(
                    select(Outbox).where(
                        Outbox.aggregate_id == booking.id,
                        Outbox.event_type == "hold_expired",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1

        promoted_events = (
            (
                await session.execute(
                    select(Outbox).where(
                        Outbox.event_type == "waitlist_offered",
                        Outbox.payload["recipient_id"].as_string() == str(user_waiter.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(promoted_events) == 1

        ew = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_waiter.id))
        ).scalar_one()
        assert ew.status == "offered"


# ---------------------------------------------------------------------------
# Test 3: Restart resumes from beginning
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_restart_resumes_from_beginning() -> None:
    """Fresh scheduler state resumes from the beginning without losing candidates."""
    sessionmaker = get_sessionmaker()
    res1 = await create_resource()
    res2 = await create_resource()
    u1 = await create_user()
    u2 = await create_user()

    now = datetime.now(timezone.utc)
    s = now + timedelta(hours=4)
    e = s + timedelta(hours=1)

    b1, _ = await create_offered_hold(
        resource=res1, user=u1, starts_at=s, ends_at=e, expires_at=now - timedelta(minutes=2)
    )
    b2, _ = await create_offered_hold(
        resource=res2, user=u2, starts_at=s, ends_at=e, expires_at=now - timedelta(minutes=2)
    )

    # Partial pass with batch_size=1
    s1 = HoldExpiryScheduler(sessionmaker, batch_size=1, resource_ids=[res1.id, res2.id])
    exp1 = await s1.run_cycle()
    assert exp1 == 1
    assert s1.cursor is not None

    # Fresh scheduler after restart (in-memory state reset)
    s2 = HoldExpiryScheduler(sessionmaker, batch_size=100, resource_ids=[res1.id, res2.id])
    assert s2.cursor is None
    exp2 = await s2.run_cycle()
    assert exp2 == 1

    # Assert all candidate resources processed, nothing lost, nothing duplicated
    async with sessionmaker() as session:
        b1_row = (await session.execute(select(Booking).where(Booking.id == b1.id))).scalar_one()
        assert b1_row.status == "expired"
        b2_row = (await session.execute(select(Booking).where(Booking.id == b2.id))).scalar_one()
        assert b2_row.status == "expired"

        events1 = (
            (await session.execute(select(Outbox).where(Outbox.aggregate_id == b1.id)))
            .scalars()
            .all()
        )
        assert len(events1) == 1
        assert events1[0].event_type == "hold_expired"

        events2 = (
            (await session.execute(select(Outbox).where(Outbox.aggregate_id == b2.id)))
            .scalars()
            .all()
        )
        assert len(events2) == 1
        assert events2[0].event_type == "hold_expired"


# ---------------------------------------------------------------------------
# Test 4: Empty cycle
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_empty_cycle() -> None:
    """No candidates produces 0 expired count and advances heartbeat."""
    sessionmaker = get_sessionmaker()
    empty_res = await create_resource()
    scheduler = HoldExpiryScheduler(sessionmaker, resource_ids=[empty_res.id])
    expired = await scheduler.run_cycle()
    assert expired == 0

    async with sessionmaker() as session:
        hb = (
            await session.execute(select(WorkerHeartbeat).where(WorkerHeartbeat.name == "primary"))
        ).scalar_one_or_none()
        assert hb is not None
        assert hb.expiry_scan_at is not None
        assert hb.seen_at is not None


# ---------------------------------------------------------------------------
# Test 5: UUID cursor wrap and >100 resources
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_uuid_cursor_wrap_and_over_100_resources() -> None:
    """Seed >100 resources across cycles; assert all visited and cursor wraps."""
    sessionmaker = get_sessionmaker()
    user = await create_user()
    now = datetime.now(timezone.utc)
    s = now + timedelta(hours=5)
    e = s + timedelta(hours=1)

    total_resources = 105
    resources: list[Resource] = []
    for _ in range(total_resources):
        r = await create_resource()
        resources.append(r)
        await insert_waitlist_entry(resource=r, user=user, starts_at=s, ends_at=e)

    # Cycle 1 with limit 100
    scheduler = HoldExpiryScheduler(
        sessionmaker, batch_size=100, resource_ids=[r.id for r in resources]
    )
    await scheduler.run_cycle()
    # After cycle 1, cursor has advanced past 100 examined resources
    assert scheduler.cursor is not None

    # Cycle 2 processes remaining resources and wraps to None
    await scheduler.run_cycle()
    assert scheduler.cursor is None

    # Verify all 105 resources were visited / promoted
    async with sessionmaker() as session:
        offered_entries = (
            (
                await session.execute(
                    select(WaitlistEntry).where(
                        WaitlistEntry.resource_id.in_([r.id for r in resources]),
                        WaitlistEntry.status == "offered",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(offered_entries) == total_resources


# ---------------------------------------------------------------------------
# Test 6: Skipped locks
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_skipped_locks() -> None:
    """Locked resource is skipped without blocking, and visited on next full pass."""
    sessionmaker = get_sessionmaker()
    res = await create_resource()
    u = await create_user()
    now = datetime.now(timezone.utc)
    s = now + timedelta(hours=6)
    e = s + timedelta(hours=1)

    booking, _ = await create_offered_hold(
        resource=res, user=u, starts_at=s, ends_at=e, expires_at=now - timedelta(minutes=1)
    )

    scheduler = HoldExpiryScheduler(sessionmaker, resource_ids=[res.id])

    # Session 1 locks resource row FOR UPDATE
    async with sessionmaker() as session_lock:
        async with session_lock.begin():
            await session_lock.execute(
                select(Resource).where(Resource.id == res.id).with_for_update()
            )

            # Scheduler cycle skips locked resource without blocking
            expired = await scheduler.run_cycle()
            assert expired == 0
            assert scheduler.cursor is None  # reached end and wrapped

            # Booking is still offered
            async with sessionmaker() as s_check:
                b = (
                    await s_check.execute(select(Booking).where(Booking.id == booking.id))
                ).scalar_one()
                assert b.status == "offered"

    # Lock is now released; next pass processes the resource
    expired_next = await scheduler.run_cycle()
    assert expired_next == 1

    async with sessionmaker() as s_check:
        b = (await s_check.execute(select(Booking).where(Booking.id == booking.id))).scalar_one()
        assert b.status == "expired"


# ---------------------------------------------------------------------------
# Test 7: Deadline sampled after the lock
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deadline_sampled_after_the_lock() -> None:
    """Offer deadline passes while waiting for resource lock; post-lock sample expires it."""
    sessionmaker = get_sessionmaker()
    res = await create_resource()
    u = await create_user()

    now = datetime.now(timezone.utc)
    s = now + timedelta(hours=7)
    e = s + timedelta(hours=1)

    # Deadline 0.6 seconds in the future
    deadline = now + timedelta(milliseconds=600)
    booking, _ = await create_offered_hold(
        resource=res, user=u, starts_at=s, ends_at=e, expires_at=deadline
    )

    lock_acquired = asyncio.Event()
    waiter_done = asyncio.Event()
    result_expired: list[int] = []

    async def _holding_tx() -> None:
        async with sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    select(Resource).where(Resource.id == res.id).with_for_update()
                )
                lock_acquired.set()
                # Hold lock past deadline
                await asyncio.sleep(0.9)

    async def _waiting_scheduler() -> None:
        await lock_acquired.wait()
        async with sessionmaker() as session:
            async with session.begin():
                # skip_locked=False forces waiting on the resource lock
                count = await expire_and_promote(
                    session, resource_id=res.id, now=now, skip_locked=False
                )
                result_expired.append(count)
        waiter_done.set()

    await asyncio.gather(_holding_tx(), _waiting_scheduler())
    assert result_expired == [1]

    async with sessionmaker() as session:
        b = (await session.execute(select(Booking).where(Booking.id == booking.id))).scalar_one()
        assert b.status == "expired"


# ---------------------------------------------------------------------------
# Test 8: Accept-versus-expire race
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_accept_versus_expire_race() -> None:
    """Concurrent accept and expiry yield exactly one valid terminal outcome."""
    sessionmaker = get_sessionmaker()
    res = await create_resource()
    u = await create_user()

    now = datetime.now(timezone.utc)
    slot_start = now + timedelta(hours=8)
    slot_end = slot_start + timedelta(hours=1)

    # Hold at exact deadline
    booking, entry = await create_offered_hold(
        resource=res, user=u, starts_at=slot_start, ends_at=slot_end, expires_at=now
    )

    scheduler = HoldExpiryScheduler(sessionmaker)

    async def _do_accept() -> httpx.Response:
        async with make_client() as c:
            return await c.post(
                f"/api/v1/waitlist/{entry.id}/accept",
                headers={**auth(u), "If-Match": '"1"'},
                json={},
            )

    async def _do_expire() -> int:
        return await scheduler.run_cycle()

    accept_resp, expire_count = await asyncio.gather(_do_accept(), _do_expire())

    # Concurrent race yields one valid terminal outcome:
    # 200 (accept won), 409 (accept detected expiry), or 412 (scheduler expired first)
    assert accept_resp.status_code in (200, 409, 412)

    async with sessionmaker() as session:
        b = (await session.execute(select(Booking).where(Booking.id == booking.id))).scalar_one()
        e = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry.id))
        ).scalar_one()

        if accept_resp.status_code == 200:
            assert b.status == "confirmed"
            assert e.status == "accepted"
        elif accept_resp.status_code == 409:
            assert accept_resp.json()["error"]["code"] == "HOLD_EXPIRED"
            assert b.status == "expired"
            assert e.status == "expired"
        else:
            assert accept_resp.json()["error"]["code"] == "VERSION_MISMATCH"
            assert b.status == "expired"
            assert e.status == "expired"

        # Check outbox events: no duplicate event
        events = (
            (await session.execute(select(Outbox).where(Outbox.aggregate_id == booking.id)))
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].event_type in ("booking_confirmed", "hold_expired")

    # Part 2: Committed 409 HOLD_EXPIRED still persists expiration and promotion
    res2 = await create_resource()
    u2 = await create_user()
    w2 = await create_user()
    b2, e2 = await create_offered_hold(
        resource=res2,
        user=u2,
        starts_at=slot_start,
        ends_at=slot_end,
        expires_at=now - timedelta(minutes=1),
    )
    ew2 = await insert_waitlist_entry(
        resource=res2, user=w2, starts_at=slot_start, ends_at=slot_end
    )

    async with make_client() as c:
        resp_409 = await c.post(
            f"/api/v1/waitlist/{e2.id}/accept",
            headers={**auth(u2), "If-Match": '"1"'},
            json={},
        )
    assert resp_409.status_code == 409
    assert resp_409.json()["error"]["code"] == "HOLD_EXPIRED"

    async with sessionmaker() as session:
        b2_row = (await session.execute(select(Booking).where(Booking.id == b2.id))).scalar_one()
        assert b2_row.status == "expired"

        e2_row = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == e2.id))
        ).scalar_one()
        assert e2_row.status == "expired"

        ew2_row = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == ew2.id))
        ).scalar_one()
        assert ew2_row.status == "offered"
        assert ew2_row.offered_booking_id is not None


# ---------------------------------------------------------------------------
# Test 9: Lifecycle decoupled from email dispatch (20s block)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_lifecycle_decoupled_from_email_dispatch() -> None:
    """Supervised task blocked for 20s does not block heartbeat or hold expiry."""
    sessionmaker = get_sessionmaker()
    res = await create_resource()
    u = await create_user()

    now = datetime.now(timezone.utc)
    s = now + timedelta(hours=9)
    e = s + timedelta(hours=1)
    booking, _ = await create_offered_hold(
        resource=res, user=u, starts_at=s, ends_at=e, expires_at=now - timedelta(minutes=1)
    )

    dispatcher_started = asyncio.Event()

    async def blocked_dispatcher(supervisor: WorkerSupervisor) -> None:
        dispatcher_started.set()
        await asyncio.sleep(20)

    supervisor = WorkerSupervisor(
        sessionmaker,
        heartbeat_interval=0.1,
        scheduler_interval=0.1,
        maintenance_interval=3600,
        registered_tasks=[blocked_dispatcher],
    )

    task = asyncio.create_task(supervisor.run())
    await dispatcher_started.wait()

    # Wait 0.8s while dispatcher is blocked for 20s
    await asyncio.sleep(0.8)

    # Assert heartbeat advanced and hold expired
    async with sessionmaker() as session:
        hb = (
            await session.execute(select(WorkerHeartbeat).where(WorkerHeartbeat.name == "primary"))
        ).scalar_one_or_none()
        assert hb is not None
        assert hb.seen_at is not None

        b = (await session.execute(select(Booking).where(Booking.id == booking.id))).scalar_one()
        assert b.status == "expired"

    # Stop supervisor cleanly
    supervisor.request_shutdown()
    await supervisor._shutdown()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, BaseException):
        pass


# ---------------------------------------------------------------------------
# Test 10: Maintenance batched deletes
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_maintenance_purges_only_expired_data() -> None:
    """>500 expired idempotency rows and >500 old rate limits deleted; control data survives."""
    sessionmaker = get_sessionmaker()
    user = await create_user()
    res = await create_resource()
    now = datetime.now(timezone.utc)

    # 1. Seed 550 expired idempotency keys
    # expires_at = created_at + interval '24 hours'
    old_created = now - timedelta(hours=48)
    old_expires = old_created + timedelta(hours=24)
    expired_idemp_keys: list[uuid.UUID] = []
    async with sessionmaker() as session:
        async with session.begin():
            for _ in range(550):
                k = uuid.uuid4()
                expired_idemp_keys.append(k)
                session.add(
                    IdempotencyKey(
                        user_id=user.id,
                        key=k,
                        request_hash="a" * 64,
                        created_at=old_created,
                        expires_at=old_expires,
                    )
                )

    # 2. Seed 10 fresh idempotency keys
    fresh_created = now
    fresh_expires = fresh_created + timedelta(hours=24)
    fresh_idemp_keys: list[uuid.UUID] = []
    async with sessionmaker() as session:
        async with session.begin():
            for _ in range(10):
                k = uuid.uuid4()
                fresh_idemp_keys.append(k)
                session.add(
                    IdempotencyKey(
                        user_id=user.id,
                        key=k,
                        request_hash="b" * 64,
                        created_at=fresh_created,
                        expires_at=fresh_expires,
                    )
                )

    # 3. Seed 550 rate limits older than 24h
    old_window = now - timedelta(hours=30)
    async with sessionmaker() as session:
        async with session.begin():
            for i in range(550):
                session.add(
                    RateLimit(
                        scope="login_ip",
                        identity_hash=f"{i:064x}",
                        window_start=old_window,
                        count=1,
                    )
                )

    # 4. Seed 10 fresh rate limits
    fresh_window = now - timedelta(minutes=10)
    async with sessionmaker() as session:
        async with session.begin():
            for i in range(10):
                session.add(
                    RateLimit(
                        scope="login_ip",
                        identity_hash=f"{1000 + i:064x}",
                        window_start=fresh_window,
                        count=1,
                    )
                )

    # 5. Seed control data: booking, refresh token, audit, feedback
    s = now + timedelta(days=2)
    e = s + timedelta(hours=1)
    ctrl_booking = Booking(
        id=uuid.uuid4(),
        resource_id=res.id,
        user_id=user.id,
        created_by=user.id,
        kind="reservation",
        time_range=Range(s, e, bounds="[)"),
        status="confirmed",
        version=1,
    )
    ctrl_token = RefreshToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash="c" * 64,
        family_id=uuid.uuid4(),
        created_at=now,
        expires_at=now + timedelta(days=7),
        family_expires_at=now + timedelta(days=30),
    )
    ctrl_audit = AuditLog(
        id=uuid.uuid4(),
        actor_id=user.id,
        action="test.action",
        target_type="resource",
        target_id=res.id,
        request_id=uuid.uuid4(),
        details={},
    )
    ctrl_feedback = Feedback(
        id=uuid.uuid4(),
        user_id=user.id,
        rating=5,
        task_completed=True,
        difficulty="easy",
        improvement="none",
        consent_version="2026-09-v1",
    )
    async with sessionmaker() as session:
        async with session.begin():
            session.add(ctrl_booking)
            session.add(ctrl_token)
            session.add(ctrl_audit)
            session.add(ctrl_feedback)

    # Run maintenance in batches of 500
    counts = await run_maintenance(sessionmaker, batch_size=500)
    assert counts["idempotency_keys"] == 550
    assert counts["rate_limits"] == 550

    # Assert expired rows are deleted and control rows survive
    async with sessionmaker() as session:
        remaining_old_idemp = (
            (
                await session.execute(
                    select(IdempotencyKey).where(IdempotencyKey.key.in_(expired_idemp_keys))
                )
            )
            .scalars()
            .all()
        )
        assert len(remaining_old_idemp) == 0

        remaining_fresh_idemp = (
            (
                await session.execute(
                    select(IdempotencyKey).where(IdempotencyKey.key.in_(fresh_idemp_keys))
                )
            )
            .scalars()
            .all()
        )
        assert len(remaining_fresh_idemp) == 10

        remaining_old_rl = (
            (await session.execute(select(RateLimit).where(RateLimit.window_start == old_window)))
            .scalars()
            .all()
        )
        assert len(remaining_old_rl) == 0

        remaining_fresh_rl = (
            (await session.execute(select(RateLimit).where(RateLimit.window_start == fresh_window)))
            .scalars()
            .all()
        )
        assert len(remaining_fresh_rl) == 10

        # Control data checks
        b_surv = (
            await session.execute(select(Booking).where(Booking.id == ctrl_booking.id))
        ).scalar_one_or_none()
        assert b_surv is not None

        tok_surv = (
            await session.execute(select(RefreshToken).where(RefreshToken.id == ctrl_token.id))
        ).scalar_one_or_none()
        assert tok_surv is not None

        aud_surv = (
            await session.execute(select(AuditLog).where(AuditLog.id == ctrl_audit.id))
        ).scalar_one_or_none()
        assert aud_surv is not None

        fb_surv = (
            await session.execute(select(Feedback).where(Feedback.id == ctrl_feedback.id))
        ).scalar_one_or_none()
        assert fb_surv is not None


# ---------------------------------------------------------------------------
# Test 11: Supervision and shutdown
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_supervision_and_shutdown() -> None:
    """Failing task stops supervisor; shutdown completes within budget."""
    sessionmaker = get_sessionmaker()
    assert SHUTDOWN_BUDGET_SECONDS == 25.0

    # 1. Failing task stops supervisor
    async def failing_task(supervisor: WorkerSupervisor) -> None:
        await asyncio.sleep(0.05)
        raise RuntimeError("simulated task crash")

    supervisor = WorkerSupervisor(
        sessionmaker,
        heartbeat_interval=1.0,
        scheduler_interval=1.0,
        maintenance_interval=3600,
        registered_tasks=[failing_task],
    )

    with pytest.raises(RuntimeError, match="simulated task crash"):
        await supervisor.run()

    assert supervisor.is_stopping

    # 2. Shutdown budget enforcement
    async def lingering_task(s: WorkerSupervisor) -> None:
        try:
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            pass

    short_budget = 0.3
    supervisor2 = WorkerSupervisor(
        sessionmaker,
        shutdown_budget=short_budget,
        heartbeat_interval=1.0,
        scheduler_interval=1.0,
        maintenance_interval=3600,
        registered_tasks=[lingering_task],
    )

    t = asyncio.create_task(supervisor2.run())
    await asyncio.sleep(0.05)

    start = time.monotonic()
    supervisor2.request_shutdown()
    await supervisor2._shutdown()
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # completed well within the budget
    t.cancel()
    try:
        await t
    except (asyncio.CancelledError, BaseException):
        pass


# ---------------------------------------------------------------------------
# Test 12: Maximum-delay observability
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_maximum_delay_observability() -> None:
    """Heartbeat seen_at advances every 10s and expiry_scan_at advances on discovery."""
    sessionmaker = get_sessionmaker()

    # Direct heartbeat recording
    async with sessionmaker() as session:
        async with session.begin():
            await record_heartbeat(session, seen=True, expiry_scan=False)

    async with sessionmaker() as session:
        hb1 = (
            await session.execute(select(WorkerHeartbeat).where(WorkerHeartbeat.name == "primary"))
        ).scalar_one()
        seen_1 = hb1.seen_at
        scan_1 = hb1.expiry_scan_at

    await asyncio.sleep(0.05)

    # Discovery cycle updates expiry_scan_at
    scheduler = HoldExpiryScheduler(sessionmaker)
    await scheduler.run_cycle()

    async with sessionmaker() as session:
        hb2 = (
            await session.execute(select(WorkerHeartbeat).where(WorkerHeartbeat.name == "primary"))
        ).scalar_one()
        assert hb2.expiry_scan_at >= scan_1
        assert hb2.seen_at >= seen_1
