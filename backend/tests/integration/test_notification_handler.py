"""Integration tests for notification handler idempotency and receipt tracking.

Ticket: T-018
Spec: 6.1, 6.3, 8.3
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

import app.db.session
from app.auth.models import User
from app.auth.passwords import hash_password
from app.bookings.models import Booking
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.notifications.adapters import EmailAdapter, EmailDeliveryError, EmailMessage
from app.notifications.handler import _dispatch_event, dispatch_event
from app.notifications.models import NotificationDelivery, Outbox
from app.notifications.outbox import (
    append_event,
    claim_batch,
    compute_backoff_seconds,
    recover_stale_leases,
)
from app.resources.models import Resource

get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _cleanup_engine() -> Any:
    yield
    if app.db.session._engine is not None:
        await app.db.session._engine.dispose()
        app.db.session._engine = None
        app.db.session._sessionmaker = None
        app.db.session._engine_url = None


@pytest.fixture(autouse=True)
async def _isolate_outbox() -> Any:
    from sqlalchemy import delete

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


class RecordingEmailAdapter:
    """Mock adapter recording sent messages and simulating latency or connection checks."""

    def __init__(self) -> None:
        self.sent_messages: list[EmailMessage] = []
        self.checked_out_during_send: list[int] = []

    async def send(self, message: EmailMessage) -> str:
        engine = app.db.session._engine
        if engine is not None:
            # Record number of checked-out connections during SMTP send
            pool = getattr(getattr(engine, "sync_engine", None), "pool", None)
            checked = pool.checkedout() if pool is not None else 0
            self.checked_out_during_send.append(checked)
        self.sent_messages.append(message)
        return f"provider-msg-{uuid.uuid4().hex[:12]}"


async def _seed_user_and_resource(*, enabled: bool = True) -> tuple[User, Resource]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            user = User(
                id=uuid.uuid4(),
                email=f"user_{uuid.uuid4().hex[:8]}@example.com",
                password_hash=hash_password("test-pass-123"),
                display_name="Notification Test User",
                role="member",
                enabled=enabled,
                version=1,
            )
            session.add(user)

            resource = Resource(
                id=uuid.uuid4(),
                name=f"Room {uuid.uuid4().hex[:6]}",
                description="Notification testing room",
                location="Building A",
                active=True,
                version=1,
            )
            session.add(resource)
    return user, resource


async def _seed_booking(
    user: User, resource: Resource, *, status: str = "confirmed", expires_at: datetime | None = None
) -> Booking:
    sessionmaker = get_sessionmaker()
    now = datetime.now(timezone.utc)
    t_start = now + timedelta(days=2)
    t_end = t_start + timedelta(hours=2)
    async with sessionmaker() as session:
        async with session.begin():
            booking = Booking(
                id=uuid.uuid4(),
                resource_id=resource.id,
                user_id=user.id,
                created_by=user.id,
                kind="reservation",
                time_range=Range(t_start, t_end, bounds="[)"),
                status=status,
                expires_at=expires_at,
                version=1,
            )
            session.add(booking)
    return booking


@pytest.mark.asyncio
async def test_duplicate_delivery_after_sent_receipt_skips_send() -> None:
    """Duplicate delivery after a 'sent' receipt skips re-sending email."""
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

    adapter = RecordingEmailAdapter()
    await _dispatch_event(lease, adapter, sessionmaker=sessionmaker, now=now)

    assert len(adapter.sent_messages) == 1
    assert adapter.sent_messages[0].to == user.email
    assert adapter.sent_messages[0].message_id == f"<{lease.id}.{user.id}@commonsbook.invalid>"

    # Verify receipt state in DB is 'sent' and outbox row is 'delivered'
    async with sessionmaker() as session:
        receipt = (
            await session.execute(
                select(NotificationDelivery).where(NotificationDelivery.event_id == eid)
            )
        ).scalar_one()
        assert receipt.state == "sent"
        assert receipt.provider_message_id is not None

        outbox_row = await session.get(Outbox, eid)
        assert outbox_row is not None
        assert outbox_row.status == "delivered"

    # Redelivery attempt with the same lease: should skip send
    await _dispatch_event(lease, adapter, sessionmaker=sessionmaker, now=now)
    assert len(adapter.sent_messages) == 1  # Still 1, not sent again!


@pytest.mark.asyncio
async def test_crash_injected_after_send_allows_duplicate_email_no_domain_duplicates() -> None:
    """Crash injected after send and before receipt: duplicate email allowed, no domain dupes."""
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
    lease = leases[0]

    adapter = RecordingEmailAdapter()

    # Create a wrapper adapter that crashes AFTER sending message 1
    class CrashingAdapter(EmailAdapter):
        def __init__(self, inner: RecordingEmailAdapter) -> None:
            self.inner = inner
            self.crashed = False

        async def send(self, message: EmailMessage) -> str:
            msg_id = await self.inner.send(message)
            if not self.crashed:
                self.crashed = True
                # Simulate worker process crash immediately after SMTP response received
                raise SystemExit("Simulated process crash after SMTP send")
            return msg_id

    crashing_adapter = CrashingAdapter(adapter)

    with pytest.raises(SystemExit):
        await _dispatch_event(lease, crashing_adapter, sessionmaker=sessionmaker, now=now)

    # Message was sent to the provider
    assert len(adapter.sent_messages) == 1

    # In DB: outbox is still 'processing', receipt was not yet marked 'sent'
    async with sessionmaker() as session:
        outbox_row = await session.get(Outbox, eid)
        assert outbox_row is not None
        assert outbox_row.status == "processing"

    # Advance time past lease expiration (61s) and run stale lease recovery
    now_recovered = now + timedelta(seconds=61)
    async with sessionmaker() as session:
        async with session.begin():
            await recover_stale_leases(session, now=now_recovered)

    # Claim the lease again
    async with sessionmaker() as session:
        reclaimed_leases = await claim_batch(session, now=now_recovered)
    assert len(reclaimed_leases) == 1
    reclaimed_lease = reclaimed_leases[0]

    # Dispatch again: now completes successfully
    await _dispatch_event(
        reclaimed_lease, crashing_adapter, sessionmaker=sessionmaker, now=now_recovered
    )

    # 2 emails were sent (allowable at-least-once SMTP duplicate on crash)
    assert len(adapter.sent_messages) == 2

    # Assert NO domain duplicates in DB:
    async with sessionmaker() as session:
        # Exactly 1 receipt in notification_deliveries
        receipts = (
            (
                await session.execute(
                    select(NotificationDelivery).where(NotificationDelivery.event_id == eid)
                )
            )
            .scalars()
            .all()
        )
        assert len(receipts) == 1
        assert receipts[0].state == "sent"

        # Exactly 1 outbox event
        outbox_rows = (
            (await session.execute(select(Outbox).where(Outbox.id == eid))).scalars().all()
        )
        assert len(outbox_rows) == 1
        assert outbox_rows[0].status == "delivered"

        # Exactly 1 booking
        bookings = (
            (await session.execute(select(Booking).where(Booking.id == booking.id))).scalars().all()
        )
        assert len(bookings) == 1


@pytest.mark.asyncio
async def test_disabled_recipient_skipped() -> None:
    """Disabled recipient causes notification to be skipped without sending email."""
    # Seed disabled user
    user, resource = await _seed_user_and_resource(enabled=False)
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
    lease = leases[0]

    adapter = RecordingEmailAdapter()
    await _dispatch_event(lease, adapter, sessionmaker=sessionmaker, now=now)

    # Email was NOT sent
    assert len(adapter.sent_messages) == 0

    # Receipt is 'skipped' and outbox row is 'delivered'
    async with sessionmaker() as session:
        receipt = (
            await session.execute(
                select(NotificationDelivery).where(NotificationDelivery.event_id == eid)
            )
        ).scalar_one()
        assert receipt.state == "skipped"

        outbox_row = await session.get(Outbox, eid)
        assert outbox_row is not None
        assert outbox_row.status == "delivered"


@pytest.mark.asyncio
async def test_stale_or_expired_waitlist_offered_skipped() -> None:
    """A waitlist_offered event whose linked booking is no longer offered or expired is skipped."""
    user, resource = await _seed_user_and_resource()
    # Seed booking with status='confirmed' (no longer 'offered')
    booking = await _seed_booking(user, resource, status="confirmed")
    sessionmaker = get_sessionmaker()
    now = datetime.now(timezone.utc)

    async with sessionmaker() as session:
        async with session.begin():
            eid = await append_event(
                session, event_type="waitlist_offered", booking=booking, now=now
            )

    async with sessionmaker() as session:
        leases = await claim_batch(session, now=now)
    lease = leases[0]

    adapter = RecordingEmailAdapter()
    await _dispatch_event(lease, adapter, sessionmaker=sessionmaker, now=now)

    # Email NOT sent because booking is not offered
    assert len(adapter.sent_messages) == 0

    async with sessionmaker() as session:
        receipt = (
            await session.execute(
                select(NotificationDelivery).where(NotificationDelivery.event_id == eid)
            )
        ).scalar_one()
        assert receipt.state == "skipped"

        outbox_row = await session.get(Outbox, eid)
        assert outbox_row is not None
        assert outbox_row.status == "delivered"


@pytest.mark.asyncio
async def test_no_db_connection_held_during_smtp() -> None:
    """The SMTP adapter send holds no database connection."""
    user, resource = await _seed_user_and_resource()
    booking = await _seed_booking(user, resource)
    sessionmaker = get_sessionmaker()
    now = datetime.now(timezone.utc)

    async with sessionmaker() as session:
        async with session.begin():
            await append_event(session, event_type="booking_confirmed", booking=booking, now=now)

    async with sessionmaker() as session:
        leases = await claim_batch(session, now=now)
    lease = leases[0]

    adapter = RecordingEmailAdapter()
    await _dispatch_event(lease, adapter, sessionmaker=sessionmaker, now=now)

    assert len(adapter.sent_messages) == 1
    # Check that checked-out connections count was 0 during adapter.send
    assert adapter.checked_out_during_send == [0]


@pytest.mark.asyncio
async def test_receipt_uniqueness_under_concurrency() -> None:
    """Receipt uniqueness is preserved under concurrent dispatch attempts."""
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
    lease = leases[0]

    adapter = RecordingEmailAdapter()

    # Run two dispatch_event calls concurrently for the exact same lease
    await asyncio.gather(
        _dispatch_event(lease, adapter, sessionmaker=sessionmaker, now=now),
        _dispatch_event(lease, adapter, sessionmaker=sessionmaker, now=now),
    )

    # Exactly 1 receipt in notification_deliveries
    async with sessionmaker() as session:
        receipts = (
            (
                await session.execute(
                    select(NotificationDelivery).where(NotificationDelivery.event_id == eid)
                )
            )
            .scalars()
            .all()
        )
        assert len(receipts) == 1
        assert receipts[0].state == "sent"


@pytest.mark.asyncio
async def test_dispatch_event_public_signature_two_args() -> None:
    """Fixed Spec 3.4 public signature dispatch_event(lease, adapter) takes 2 args."""
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
    lease = leases[0]

    adapter = RecordingEmailAdapter()
    # Call the exact public 2-argument signature
    await dispatch_event(lease, adapter)

    assert len(adapter.sent_messages) == 1
    async with sessionmaker() as session:
        outbox_row = await session.get(Outbox, eid)
        assert outbox_row is not None
        assert outbox_row.status == "delivered"


@pytest.mark.asyncio
async def test_adapter_delivery_error_transitions_to_pending_with_backoff_and_last_error() -> None:
    """R1: EmailDeliveryError commits transition to pending with backoff and last_error."""
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
    lease = leases[0]

    class FailingAdapter(EmailAdapter):
        async def send(self, message: EmailMessage) -> str:
            raise EmailDeliveryError("timeout")

    failing_adapter = FailingAdapter()
    await _dispatch_event(lease, failing_adapter, sessionmaker=sessionmaker, now=now)

    # R1 assertion: Outbox row MUST NOT remain processing; it must be committed to 'pending'
    async with sessionmaker() as session:
        outbox_row = await session.get(Outbox, eid)
        assert outbox_row is not None
        assert outbox_row.status == "pending", (
            f"Expected row to transition to pending, got {outbox_row.status}"
        )
        assert outbox_row.last_error == "timeout"
        assert outbox_row.lease_token is None
        assert outbox_row.lease_until is None

        # Verify backoff availability (allowing subsecond DB clock skew from R6)
        expected_backoff = compute_backoff_seconds(lease.id, lease.attempts)
        expected_available = now + timedelta(seconds=expected_backoff)
        assert abs((outbox_row.available_at - expected_available).total_seconds()) < 2.0

        # Before expected_available, row is NOT claimed
        early_leases = await claim_batch(session, now=now + timedelta(seconds=1))
        assert not any(item.id == eid for item in early_leases)

        # At available_at, row IS claimed
        due_leases = await claim_batch(session, now=outbox_row.available_at)
        assert any(item.id == eid for item in due_leases)


@pytest.mark.asyncio
async def test_adapter_unexpected_exception_transitions_to_pending_with_transport_error() -> None:
    """R1: Unexpected adapter exception commits fail_lease transition with category='transport'."""
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
    lease = leases[0]

    class ExplodingAdapter(EmailAdapter):
        async def send(self, message: EmailMessage) -> str:
            raise RuntimeError("Unexpected network socket explosion")

    exploding_adapter = ExplodingAdapter()
    await _dispatch_event(lease, exploding_adapter, sessionmaker=sessionmaker, now=now)

    async with sessionmaker() as session:
        outbox_row = await session.get(Outbox, eid)
        assert outbox_row is not None
        assert outbox_row.status == "pending"
        assert outbox_row.last_error == "transport"
        assert outbox_row.lease_token is None
        assert outbox_row.lease_until is None
