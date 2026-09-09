"""Integration tests for durable outbox leasing, token-fenced ack/fail, and recovery.

Ticket: T-018
Spec: 3.2, 3.4, 6.1, 6.2
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import Range

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.auth.models import User
from app.auth.passwords import hash_password
from app.bookings.models import Booking
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.notifications.models import Outbox
from app.notifications.outbox import (
    OutboxLease,
    acknowledge,
    append_event,
    claim_batch,
    compute_backoff_seconds,
    fail_lease,
    recover_stale_leases,
)
from app.resources.models import Resource

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


@pytest.fixture(autouse=True)
async def _isolate_outbox() -> Any:
    from sqlalchemy import delete

    from app.notifications.models import NotificationDelivery

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            await session.execute(delete(NotificationDelivery))
            await session.execute(delete(Outbox))
    yield
    async with sessionmaker() as session:
        async with session.begin():
            await session.execute(delete(NotificationDelivery))
            await session.execute(delete(Outbox))


async def _seed_user_and_resource() -> tuple[User, Resource]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            user = User(
                id=uuid.uuid4(),
                email=f"user_{uuid.uuid4().hex[:8]}@example.com",
                password_hash=hash_password("test-pass-123"),
                display_name="Outbox Test User",
                role="member",
                enabled=True,
                version=1,
            )
            session.add(user)

            resource = Resource(
                id=uuid.uuid4(),
                name=f"Resource {uuid.uuid4().hex[:6]}",
                description="Outbox testing resource",
                location="Room 101",
                active=True,
                version=1,
            )
            session.add(resource)
    return user, resource


async def _seed_booking(user: User, resource: Resource, offset_hours: int = 0) -> Booking:
    sessionmaker = get_sessionmaker()
    now = datetime.now(timezone.utc)
    t_start = now + timedelta(days=1, hours=offset_hours)
    t_end = t_start + timedelta(hours=1)
    async with sessionmaker() as session:
        async with session.begin():
            booking = Booking(
                id=uuid.uuid4(),
                resource_id=resource.id,
                user_id=user.id,
                created_by=user.id,
                kind="reservation",
                time_range=Range(t_start, t_end, bounds="[)"),
                status="confirmed",
                expires_at=None,
                version=1,
            )
            session.add(booking)
    return booking


@pytest.mark.asyncio
async def test_concurrent_claim_batch_disjoint_rows() -> None:
    """Independent concurrent claimers get disjoint rows and never double-claim."""
    user, resource = await _seed_user_and_resource()
    sessionmaker = get_sessionmaker()

    # Create 5 bookings and 5 outbox events
    now = datetime.now(timezone.utc)
    outbox_ids = []
    for i in range(5):
        booking = await _seed_booking(user, resource, offset_hours=2 * i)
        async with sessionmaker() as session:
            async with session.begin():
                eid = await append_event(
                    session,
                    event_type="booking_confirmed",
                    booking=booking,
                    now=now - timedelta(seconds=10),
                )
                outbox_ids.append(eid)

    # 5 concurrent claimers each with its own AsyncSession
    async def _claim_one() -> list[OutboxLease]:
        async with sessionmaker() as session:
            return await claim_batch(session, now=now)

    tasks = [_claim_one() for _ in range(5)]
    results = await asyncio.gather(*tasks)

    claimed_leases: list[OutboxLease] = [r[0] for r in results if r]
    # Exactly 5 leases claimed total
    assert len(claimed_leases) == 5

    # All row IDs are disjoint
    claimed_ids = {lease.id for lease in claimed_leases}
    assert claimed_ids == set(outbox_ids)

    # All lease tokens are unique UUIDs
    claimed_tokens = {lease.lease_token for lease in claimed_leases}
    assert len(claimed_tokens) == 5

    # Verify rows in PostgreSQL are all 'processing' with attempts=1
    async with sessionmaker() as session:
        for eid in outbox_ids:
            row = await session.get(Outbox, eid)
            assert row is not None
            assert row.status == "processing"
            assert row.attempts == 1
            assert row.lease_token is not None
            assert row.lease_until is not None


@pytest.mark.asyncio
async def test_lost_ownership_acknowledge_returns_false() -> None:
    """A lost-ownership acknowledge returns False and changes nothing."""
    user, resource = await _seed_user_and_resource()
    booking = await _seed_booking(user, resource)
    sessionmaker = get_sessionmaker()
    now = datetime.now(timezone.utc)

    async with sessionmaker() as session:
        async with session.begin():
            eid = await append_event(
                session, event_type="booking_confirmed", booking=booking, now=now
            )

    # Claim the row
    async with sessionmaker() as session:
        leases = await claim_batch(session, now=now)
    assert len(leases) == 1
    lease = leases[0]

    # Stale worker simulation: a different worker acquired a new lease token
    different_token = uuid.uuid4()
    async with sessionmaker() as session:
        async with session.begin():
            await session.execute(
                update(Outbox)
                .where(Outbox.id == eid)
                .values(
                    status="processing",
                    lease_token=different_token,
                    lease_until=now + timedelta(seconds=60),
                )
            )

    # Original worker attempts to acknowledge with old token
    async with sessionmaker() as session:
        async with session.begin():
            ack_res = await acknowledge(session, lease=lease, now=now)

    assert ack_res is False

    # Check that database row was NOT delivered and retains different_token
    async with sessionmaker() as session:
        row = await session.get(Outbox, eid)
        assert row is not None
        assert row.status == "processing"
        assert row.lease_token == different_token
        assert row.delivered_at is None


@pytest.mark.asyncio
async def test_lost_ownership_fail_lease_returns_false() -> None:
    """A lost-ownership fail_lease returns False and changes nothing."""
    user, resource = await _seed_user_and_resource()
    booking = await _seed_booking(user, resource)
    sessionmaker = get_sessionmaker()
    now = datetime.now(timezone.utc)

    async with sessionmaker() as session:
        async with session.begin():
            eid = await append_event(
                session, event_type="booking_confirmed", booking=booking, now=now
            )

    async with sessionmaker() as session:
        leases = await claim_batch(session, now=now)
    assert len(leases) == 1
    lease = leases[0]

    # Simulate another worker taking over with a different lease token
    different_token = uuid.uuid4()
    async with sessionmaker() as session:
        async with session.begin():
            await session.execute(
                update(Outbox)
                .where(Outbox.id == eid)
                .values(
                    status="processing",
                    lease_token=different_token,
                    lease_until=now + timedelta(seconds=60),
                )
            )

    # Stale worker attempts fail_lease
    async with sessionmaker() as session:
        async with session.begin():
            fail_res = await fail_lease(session, lease=lease, error_category="transport", now=now)

    assert fail_res is False

    # Check that row was unchanged
    async with sessionmaker() as session:
        row = await session.get(Outbox, eid)
        assert row is not None
        assert row.status == "processing"
        assert row.lease_token == different_token
        assert row.last_error is None


@pytest.mark.asyncio
async def test_process_crash_leaves_row_processing_until_stale_recovery() -> None:
    """Process crash simulation leaves row processing until stale-lease recovery returns it."""
    user, resource = await _seed_user_and_resource()
    booking = await _seed_booking(user, resource)
    sessionmaker = get_sessionmaker()
    t0 = datetime.now(timezone.utc)

    async with sessionmaker() as session:
        async with session.begin():
            eid = await append_event(
                session, event_type="booking_confirmed", booking=booking, now=t0
            )

    # Worker claims the row
    async with sessionmaker() as session:
        leases = await claim_batch(session, now=t0)
    assert len(leases) == 1

    # Worker process crashes: session ends without acknowledge or fail_lease
    # Verify row in DB is in processing state with lease_until = t0 + 60s
    async with sessionmaker() as session:
        row = await session.get(Outbox, eid)
        assert row is not None
        assert row.status == "processing"
        assert row.lease_token is not None

    # Before lease expires (t0 + 30s), stale recovery does not touch this row
    async with sessionmaker() as session:
        async with session.begin():
            await recover_stale_leases(session, now=t0 + timedelta(seconds=30))
        row_30 = await session.get(Outbox, eid)
        assert row_30 is not None
        assert row_30.status == "processing"

    # After lease expires (t0 + 61s), stale recovery resets it to pending
    async with sessionmaker() as session:
        async with session.begin():
            await recover_stale_leases(session, now=t0 + timedelta(seconds=61))
        row_61 = await session.get(Outbox, eid)
        assert row_61 is not None
        assert row_61.status == "pending"

    # Row is now pending again, lease fields cleared, ready to be re-claimed
    async with sessionmaker() as session:
        row = await session.get(Outbox, eid)
        assert row is not None
        assert row.status == "pending"
        assert row.lease_token is None
        assert row.lease_until is None

        # Another worker can now claim it
        new_leases = await claim_batch(session, now=t0 + timedelta(seconds=62))
        assert len(new_leases) == 1
        assert new_leases[0].id == eid
        assert new_leases[0].attempts == 2


@pytest.mark.asyncio
async def test_exhausted_retries_reach_dead_and_are_not_reclaimed() -> None:
    """Exhausted retries (attempts >= 8) reach 'dead' and are never re-claimed."""
    user, resource = await _seed_user_and_resource()
    booking = await _seed_booking(user, resource)
    sessionmaker = get_sessionmaker()
    now = datetime.now(timezone.utc)

    async with sessionmaker() as session:
        async with session.begin():
            eid = await append_event(
                session, event_type="booking_confirmed", booking=booking, now=now
            )
            # Set attempts to 7 so the next claim makes it 8
            await session.execute(update(Outbox).where(Outbox.id == eid).values(attempts=7))

    # Claim: attempts becomes 8
    async with sessionmaker() as session:
        leases = await claim_batch(session, now=now)
    assert len(leases) == 1
    lease = leases[0]
    assert lease.attempts == 8

    # Fail the lease at attempt 8
    async with sessionmaker() as session:
        async with session.begin():
            ok = await fail_lease(session, lease=lease, error_category="transport", now=now)
    assert ok is True

    # Row is now dead
    async with sessionmaker() as session:
        row = await session.get(Outbox, eid)
        assert row is not None
        assert row.status == "dead"
        assert row.lease_token is None
        assert row.lease_until is None
        assert row.last_error == "transport"

        # Verify claim_batch never claims dead rows even far in the future
        far_future = now + timedelta(days=365)
        future_leases = await claim_batch(session, now=far_future)
        assert not any(item.id == eid for item in future_leases)

    # Also verify stale recovery on an expired processing row at attempts >= 8 marks it dead
    async with sessionmaker() as session:
        async with session.begin():
            eid_stale = await append_event(
                session, event_type="booking_cancelled", booking=booking, now=now
            )
            await session.execute(
                update(Outbox)
                .where(Outbox.id == eid_stale)
                .values(
                    status="processing",
                    attempts=8,
                    lease_token=uuid.uuid4(),
                    lease_until=now - timedelta(seconds=10),
                )
            )

    async with sessionmaker() as session:
        async with session.begin():
            recovered = await recover_stale_leases(session, now=now)
        assert recovered >= 1
        stale_row = await session.get(Outbox, eid_stale)
        assert stale_row is not None
        assert stale_row.status == "dead"
        assert stale_row.lease_token is None


@pytest.mark.asyncio
async def test_claim_commits_before_dispatch_recoverable_after_crash() -> None:
    """claim commits before dispatch so a crash between claim and dispatch is recoverable."""
    user, resource = await _seed_user_and_resource()
    booking = await _seed_booking(user, resource)
    sessionmaker = get_sessionmaker()
    now = datetime.now(timezone.utc)

    async with sessionmaker() as session:
        async with session.begin():
            eid = await append_event(
                session, event_type="booking_confirmed", booking=booking, now=now
            )

    # Worker session claims the row
    worker_session = sessionmaker()
    leases = await claim_batch(worker_session, now=now)
    assert len(leases) == 1
    lease = leases[0]

    # Simulate sudden crash: discard worker_session without calling commit() or rollback()
    await worker_session.close()

    # In a completely separate session, check the database state
    async with sessionmaker() as fresh_session:
        row = await fresh_session.get(Outbox, eid)
        assert row is not None
        # Must be persisted as processing with the lease token on disk!
        assert row.status == "processing"
        assert row.lease_token == lease.lease_token
        assert row.attempts == 1
        assert row.lease_until is not None


@pytest.mark.asyncio
async def test_transaction_durability_of_available_at() -> None:
    """Transaction durability of available_at after backoff calculation."""
    user, resource = await _seed_user_and_resource()
    booking = await _seed_booking(user, resource)
    sessionmaker = get_sessionmaker()
    now = datetime.now(timezone.utc)

    async with sessionmaker() as session:
        async with session.begin():
            eid = await append_event(
                session, event_type="booking_confirmed", booking=booking, now=now
            )

    # Claim attempt 1
    async with sessionmaker() as session:
        leases = await claim_batch(session, now=now)
    lease = leases[0]

    # Fail attempt 1
    async with sessionmaker() as session:
        async with session.begin():
            ok = await fail_lease(session, lease=lease, error_category="timeout", now=now)
    assert ok is True

    # In a fresh session, verify available_at matches the exact backoff formula
    expected_backoff = compute_backoff_seconds(lease.id, 1)
    expected_available = now + timedelta(seconds=expected_backoff)

    async with sessionmaker() as session:
        row = await session.get(Outbox, eid)
        assert row is not None
        assert row.status == "pending"
        assert row.available_at == expected_available

        # At now + 1s, claim_batch must NOT return it
        too_early_leases = await claim_batch(session, now=now + timedelta(seconds=1))
        assert not any(item.id == eid for item in too_early_leases)

        # At expected_available, claim_batch returns it
        due_leases = await claim_batch(session, now=expected_available)
        assert any(item.id == eid for item in due_leases)
