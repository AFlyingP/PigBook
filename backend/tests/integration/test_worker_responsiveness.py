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
    test_settings = Settings.model_construct(
        APP_ENV="test",
        EMAIL_ENABLED=True,
        EMAIL_ADAPTER="console",
    )

    supervisor = WorkerSupervisor(
        sessionmaker=sessionmaker,
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

        # Wait 4 seconds for heartbeat loop and scheduler loop to run concurrently
        await asyncio.sleep(4.0)

        # Prover checkpoint 2: While STILL BLOCKED in adapter.send:
        assert not blocked_adapter.send_finished.is_set(), (
            "Adapter must still be blocked during responsiveness verification"
        )

        # Checkpoint 2a: Heartbeat has advanced
        async with sessionmaker() as session:
            current_hb = await session.get(WorkerHeartbeat, "primary")
            assert current_hb is not None
            assert current_hb.seen_at > initial_seen_at, (
                f"Heartbeat seen_at did not advance: {current_hb.seen_at} <= {initial_seen_at}"
            )

        # Checkpoint 2b: Overdue hold was expired by the scheduler
        async with sessionmaker() as session:
            expired_booking = await session.get(Booking, b_overdue.id)
            assert expired_booking is not None
            assert expired_booking.status == "expired", (
                f"Overdue booking status was not expired: {expired_booking.status}"
            )

            expired_waitlist = await session.get(WaitlistEntry, w_entry.id)
            assert expired_waitlist is not None
            assert expired_waitlist.status == "expired", (
                f"Waitlist entry status was not expired: {expired_waitlist.status}"
            )

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
