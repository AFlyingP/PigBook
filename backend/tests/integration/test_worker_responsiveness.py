"""Integration test for worker responsiveness with a 20-second blocked email adapter.

Proves that due hold expiry and heartbeat updates advance while email delivery is blocked.
Ticket: T-018
Spec: 6.1, 11.6
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
from app.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.notifications.adapters import EmailAdapter, EmailMessage
from app.notifications.models import NotificationDelivery, Outbox, WorkerHeartbeat
from app.notifications.outbox import append_event
from app.resources.models import Resource
from app.waitlist.models import WaitlistEntry
from app.worker import OutboxDispatcher, WorkerSupervisor

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


class BlockedEmailAdapter(EmailAdapter):
    """Email adapter that simulates a slow provider by sleeping for 20 seconds."""

    def __init__(self, block_seconds: float = 20.0) -> None:
        self.block_seconds = block_seconds
        self.send_started = asyncio.Event()
        self.send_finished = asyncio.Event()

    async def send(self, message: EmailMessage) -> str:
        self.send_started.set()
        await asyncio.sleep(self.block_seconds)
        self.send_finished.set()
        return f"blocked-provider-msg-{uuid.uuid4().hex[:8]}"


async def _seed_user_and_resource() -> tuple[User, Resource]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            user = User(
                id=uuid.uuid4(),
                email=f"resp_user_{uuid.uuid4().hex[:8]}@example.com",
                password_hash=hash_password("test-pass-123"),
                display_name="Responsiveness User",
                role="member",
                enabled=True,
                version=1,
            )
            session.add(user)

            resource = Resource(
                id=uuid.uuid4(),
                name=f"Responsive Room {uuid.uuid4().hex[:6]}",
                description="Worker responsiveness resource",
                location="Building B",
                active=True,
                version=1,
            )
            session.add(resource)
    return user, resource


@pytest.mark.asyncio
async def test_worker_responsiveness_with_twenty_second_blocked_adapter() -> None:
    """Proves that hold expiry and heartbeat advance while email delivery is blocked for 20s."""
    user, resource = await _seed_user_and_resource()
    sessionmaker = get_sessionmaker()
    now = datetime.now(timezone.utc)

    # 1. Create a confirmed booking with an outbox event that will be claimed by dispatcher
    t_start = now + timedelta(days=5)
    t_end = t_start + timedelta(hours=1)
    async with sessionmaker() as session:
        async with session.begin():
            b_dispatch = Booking(
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
            session.add(b_dispatch)
            await session.flush()
            outbox_id = await append_event(
                session, event_type="booking_confirmed", booking=b_dispatch, now=now
            )

    # 2. Create an overdue offered booking and linked waitlist entry to be expired by scheduler
    t_due_start = now + timedelta(days=2)
    t_due_end = t_due_start + timedelta(hours=1)
    async with sessionmaker() as session:
        async with session.begin():
            b_overdue = Booking(
                id=uuid.uuid4(),
                resource_id=resource.id,
                user_id=user.id,
                created_by=user.id,
                kind="reservation",
                time_range=Range(t_due_start, t_due_end, bounds="[)"),
                status="offered",
                expires_at=now - timedelta(seconds=10),
                version=1,
            )
            session.add(b_overdue)
            await session.flush()

            w_entry = WaitlistEntry(
                id=uuid.uuid4(),
                user_id=user.id,
                resource_id=resource.id,
                time_range=Range(t_due_start, t_due_end, bounds="[)"),
                status="offered",
                offered_booking_id=b_overdue.id,
                version=1,
            )
            session.add(w_entry)

    # 3. Setup WorkerSupervisor with 20-second blocked email adapter
    blocked_adapter = BlockedEmailAdapter(block_seconds=20.0)
    test_settings = Settings(
        APP_ENV="test",
        EMAIL_ENABLED=True,
        EMAIL_ADAPTER="console",
    )

    supervisor = WorkerSupervisor(
        sessionmaker=sessionmaker,
        resource_ids=[resource.id],
        heartbeat_interval=2.0,
        scheduler_interval=2.0,
        maintenance_interval=3600.0,
        shutdown_budget=10.0,
    )

    dispatcher = OutboxDispatcher(
        sessionmaker=sessionmaker,
        adapter=blocked_adapter,
        settings=test_settings,
        poll_interval=0.5,
    )
    supervisor.register_task(dispatcher.run)

    supervisor_task = asyncio.create_task(supervisor.run())

    try:
        # Wait for email adapter to start sending and enter its 20-second sleep
        await asyncio.wait_for(blocked_adapter.send_started.wait(), timeout=10.0)

        # Prover checkpoint 1: Delivery is currently blocked!
        assert not blocked_adapter.send_finished.is_set()

        # Capture initial heartbeat seen_at
        async with sessionmaker() as session:
            initial_hb = await session.get(WorkerHeartbeat, "primary")
            assert initial_hb is not None
            initial_seen_at = initial_hb.seen_at

        # Poll while STILL BLOCKED (up to 15 seconds, well within the 20s block)
        # to observe both heartbeat progression and due hold expiry
        heartbeat_advanced = False
        hold_expired = False

        for _ in range(15):
            await asyncio.sleep(1.0)
            # Delivery must STILL be blocked on every check
            assert not blocked_adapter.send_finished.is_set(), (
                "Adapter must still be blocked during responsiveness verification"
            )
            async with sessionmaker() as session:
                if not heartbeat_advanced:
                    current_hb = await session.get(WorkerHeartbeat, "primary")
                    if current_hb and current_hb.seen_at > initial_seen_at:
                        heartbeat_advanced = True

                if not hold_expired:
                    exp_b = await session.get(Booking, b_overdue.id)
                    if exp_b and exp_b.status == "expired":
                        hold_expired = True

            if heartbeat_advanced and hold_expired:
                break

        # Prover checkpoint 2: Both occurred while send was STILL BLOCKED
        assert not blocked_adapter.send_finished.is_set(), "Adapter must still be blocked"
        assert heartbeat_advanced, "Heartbeat did not advance while delivery was blocked"
        assert hold_expired, "Overdue booking was not expired while delivery was blocked"

        # Wait for the full 20-second blocked adapter send to complete
        await asyncio.wait_for(blocked_adapter.send_finished.wait(), timeout=20.0)
        assert blocked_adapter.send_finished.is_set()

        # Allow final transaction to complete
        await asyncio.sleep(1.0)

        # Verify notification delivery receipt and outbox status
        async with sessionmaker() as session:
            receipt = (
                await session.execute(
                    select(NotificationDelivery).where(NotificationDelivery.event_id == outbox_id)
                )
            ).scalar_one_or_none()
            assert receipt is not None
            assert receipt.state == "sent"

            outbox_row = await session.get(Outbox, outbox_id)
            assert outbox_row is not None
            assert outbox_row.status == "delivered"

    finally:
        supervisor.request_shutdown()
        try:
            await asyncio.wait_for(supervisor_task, timeout=10.0)
        except Exception:
            supervisor_task.cancel()


@pytest.mark.asyncio
async def test_dispatcher_disabled_email_leaves_outbox_pending_and_lifecycle_running() -> None:
    """R4: EMAIL_ENABLED=false never invokes adapter, outbox stays pending, lifecycle runs."""
    user, resource = await _seed_user_and_resource()
    sessionmaker = get_sessionmaker()
    now = datetime.now(timezone.utc)

    # 1. Create a confirmed booking with an outbox event
    t_start = now + timedelta(days=3)
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
            await session.flush()
            outbox_id = await append_event(
                session, event_type="booking_confirmed", booking=booking, now=now
            )

    # 2. Create overdue hold to verify scheduler runs concurrently
    t_due_s = now + timedelta(days=4)
    t_due_e = t_due_s + timedelta(hours=1)
    async with sessionmaker() as session:
        async with session.begin():
            b_due = Booking(
                id=uuid.uuid4(),
                resource_id=resource.id,
                user_id=user.id,
                created_by=user.id,
                kind="reservation",
                time_range=Range(t_due_s, t_due_e, bounds="[)"),
                status="offered",
                expires_at=now - timedelta(seconds=10),
                version=1,
            )
            session.add(b_due)

    # 3. Create a tracking adapter that records invocations
    class CallTrackingAdapter(EmailAdapter):
        def __init__(self) -> None:
            self.calls: list[EmailMessage] = []

        async def send(self, message: EmailMessage) -> str:
            self.calls.append(message)
            return "tracking-msg-id"

    tracking_adapter = CallTrackingAdapter()
    disabled_settings = Settings(
        APP_ENV="test",
        EMAIL_ENABLED=False,
        EMAIL_ADAPTER="console",
    )

    supervisor = WorkerSupervisor(
        sessionmaker=sessionmaker,
        resource_ids=[resource.id],
        heartbeat_interval=0.5,
        scheduler_interval=0.5,
        maintenance_interval=3600.0,
        shutdown_budget=5.0,
    )

    dispatcher = OutboxDispatcher(
        sessionmaker=sessionmaker,
        adapter=tracking_adapter,
        settings=disabled_settings,
        poll_interval=0.2,
    )
    supervisor.register_task(dispatcher.run)

    supervisor_task = asyncio.create_task(supervisor.run())

    try:
        # Let supervisor and dispatcher run for multiple polling/heartbeat cycles
        await asyncio.sleep(2.0)

        # R4 Assertions:
        # 1. Adapter was NEVER invoked
        assert len(tracking_adapter.calls) == 0, (
            "Adapter must not be called when EMAIL_ENABLED=false"
        )

        # 2. Outbox row remains in status='pending' with attempts=0
        async with sessionmaker() as session:
            outbox_row = await session.get(Outbox, outbox_id)
            assert outbox_row is not None
            assert outbox_row.status == "pending"
            assert outbox_row.attempts == 0
            assert outbox_row.lease_token is None

            # 3. Heartbeat updated
            hb = await session.get(WorkerHeartbeat, "primary")
            assert hb is not None

            # 4. Due hold was expired by the concurrently running scheduler
            expired_booking = await session.get(Booking, b_due.id)
            assert expired_booking is not None
            assert expired_booking.status == "expired"

    finally:
        supervisor.request_shutdown()
        try:
            await asyncio.wait_for(supervisor_task, timeout=5.0)
        except Exception:
            supervisor_task.cancel()
