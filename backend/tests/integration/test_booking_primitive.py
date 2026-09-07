import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError

from alembic import command

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.auth.dependencies import AuthorizedScope, Policy
from app.auth.models import User
from app.auth.passwords import hash_password
from app.bookings.models import Booking as BookingModel
from app.bookings.schemas import Booking as BookingSchema
from app.bookings.schemas import StoredResponse
from app.bookings.service import (
    NotFoundError,
    ResourceInactive,
    SlotConflict,
    insert_confirmed,
)
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.notifications.models import Outbox
from app.notifications.outbox import append_event
from app.resources.models import Resource

get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _ensure_schema() -> None:
    """Ensure Alembic migrations have been applied to head before running test."""
    cfg = Config("backend/alembic.ini")
    await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.fixture(autouse=True)
async def _cleanup_engine() -> None:
    yield
    import app.db.session

    if app.db.session._engine is not None:
        await app.db.session._engine.dispose()
        app.db.session._engine = None
        app.db.session._sessionmaker = None
        app.db.session._engine_url = None


async def create_user(
    *,
    email: str | None = None,
    password: str = "valid-password-123",
    display_name: str = "Test User",
    role: str = "member",
    enabled: bool = True,
) -> User:
    if email is None:
        email = f"user_{uuid.uuid4().hex[:8]}@example.com"
    pwd_hash = hash_password(password)
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=pwd_hash,
        display_name=display_name,
        role=role,
        enabled=enabled,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(user)
    return user


async def create_resource(
    *,
    name: str | None = None,
    description: str = "Standard test room",
    location: str = "Building A Room 101",
    active: bool = True,
    version: int = 1,
) -> Resource:
    if name is None:
        name = f"Resource_{uuid.uuid4().hex[:8]}"
    r = Resource(
        id=uuid.uuid4(),
        name=name,
        description=description,
        location=location,
        active=active,
        version=version,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(r)
    return r


@pytest.mark.asyncio
async def test_01_happy_path_reservation_and_blackout() -> None:
    """1. Happy path: insert_confirmed returns confirmed ORM Booking; build Booking schema."""
    user = await create_user()
    admin = await create_user(role="admin")
    resource = await create_resource()

    now = datetime(2026, 7, 1, 9, 0, 0, tzinfo=timezone.utc)
    t_start = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t_end = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    sessionmaker = get_sessionmaker()

    # Reservation happy path
    scope = AuthorizedScope(principal_id=user.id, policy=Policy.authenticated)
    async with sessionmaker() as session:
        async with session.begin():
            booking_orm = await insert_confirmed(
                session,
                scope=scope,
                resource_id=resource.id,
                time_range=Range(t_start, t_end, bounds="[)"),
                kind="reservation",
                now=now,
            )
            assert booking_orm.status == "confirmed"
            assert booking_orm.user_id == user.id
            assert booking_orm.created_by == user.id
            assert booking_orm.expires_at is None
            assert booking_orm.version == 1

            # Build Spec 4.1 Booking response model from the ORM row
            resp = BookingSchema.model_validate(booking_orm)
            assert resp.id == booking_orm.id
            assert resp.resource_id == resource.id
            assert resp.user_id == user.id
            assert resp.kind == "reservation"
            assert resp.starts_at == t_start
            assert resp.ends_at == t_end
            assert resp.status == "confirmed"
            assert resp.expires_at is None
            assert resp.cancellation_reason is None
            assert resp.version == 1

            # Assert exact field set
            expected_fields = {
                "id",
                "resource_id",
                "user_id",
                "kind",
                "starts_at",
                "ends_at",
                "status",
                "expires_at",
                "cancellation_reason",
                "version",
                "created_at",
                "updated_at",
            }
            assert set(BookingSchema.model_fields.keys()) == expected_fields
            assert "created_by" not in BookingSchema.model_fields
            assert "time_range" not in BookingSchema.model_fields

    # Blackout happy path
    t_start_blackout = datetime(2026, 7, 1, 14, 0, 0, tzinfo=timezone.utc)
    t_end_blackout = datetime(2026, 7, 1, 16, 0, 0, tzinfo=timezone.utc)
    admin_scope = AuthorizedScope(principal_id=admin.id, policy=Policy.admin)
    async with sessionmaker() as session:
        async with session.begin():
            blackout_orm = await insert_confirmed(
                session,
                scope=admin_scope,
                resource_id=resource.id,
                time_range=Range(t_start_blackout, t_end_blackout, bounds="[)"),
                kind="blackout",
                now=now,
            )
            assert blackout_orm.status == "confirmed"
            assert blackout_orm.user_id is None
            assert blackout_orm.created_by == admin.id
            assert blackout_orm.expires_at is None

            blackout_resp = BookingSchema.model_validate(blackout_orm)
            assert blackout_resp.user_id is None
            assert blackout_resp.kind == "blackout"

    # Also assert StoredResponse schema
    stored = StoredResponse(status=409, body={"error": "conflict"}, headers={"Retry-After": "1"})
    assert stored.status == 409
    assert stored.body == {"error": "conflict"}
    assert stored.headers == {"Retry-After": "1"}


@pytest.mark.asyncio
async def test_02_separate_writer_overlap_rejection() -> None:
    """2. Separate-writer overlap rejection: two connections, A blocks B, A commits, B fails."""
    user = await create_user()
    resource = await create_resource()
    scope = AuthorizedScope(principal_id=user.id, policy=Policy.authenticated)

    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Overlapping interval [11:00, 13:00)
    t1_overlap = datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc)
    t2_overlap = datetime(2026, 7, 1, 13, 0, 0, tzinfo=timezone.utc)

    sessionmaker = get_sessionmaker()

    writer_a_inserted = asyncio.Event()
    writer_a_release = asyncio.Event()
    writer_b_exception = None

    async def run_writer_a() -> None:
        try:
            async with sessionmaker() as session_a:
                async with session_a.begin():
                    await insert_confirmed(
                        session_a,
                        scope=scope,
                        resource_id=resource.id,
                        time_range=Range(t1, t2, bounds="[)"),
                    )
                    writer_a_inserted.set()
                    await writer_a_release.wait()
        finally:
            writer_a_inserted.set()

    async def run_writer_b() -> None:
        nonlocal writer_b_exception
        await writer_a_inserted.wait()
        async with sessionmaker() as session_b:
            async with session_b.begin():
                try:
                    await insert_confirmed(
                        session_b,
                        scope=scope,
                        resource_id=resource.id,
                        time_range=Range(t1_overlap, t2_overlap, bounds="[)"),
                    )
                except Exception as exc:
                    writer_b_exception = exc
                    raise

    task_a = asyncio.create_task(run_writer_a())
    task_b = asyncio.create_task(run_writer_b())

    # Wait until writer A has inserted
    await writer_a_inserted.wait()
    # Give writer B time to start and block on postgres GiST exclusion lock
    await asyncio.sleep(0.1)
    assert not task_b.done(), "Writer B should be blocked waiting for Writer A transaction"

    # Release writer A to commit
    writer_a_release.set()
    await task_a

    # Writer B must finish with SlotConflict
    with pytest.raises(SlotConflict):
        await task_b

    assert isinstance(writer_b_exception, SlotConflict)

    # Invariant assertion by ROW COUNT: exactly one active row in the database
    async with sessionmaker() as session_fresh:
        stmt = select(func.count(BookingModel.id)).where(
            BookingModel.resource_id == resource.id,
            BookingModel.status == "confirmed",
        )
        count = (await session_fresh.execute(stmt)).scalar_one()
        assert count == 1


@pytest.mark.asyncio
async def test_03_adjacency_succeeds() -> None:
    """3. Adjacency succeeds: [10:00, 11:00) and [11:00, 12:00) on same resource both commit."""
    user = await create_user()
    resource = await create_resource()
    scope = AuthorizedScope(principal_id=user.id, policy=Policy.authenticated)

    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session1:
        async with session1.begin():
            await insert_confirmed(
                session1,
                scope=scope,
                resource_id=resource.id,
                time_range=Range(t1, t2, bounds="[)"),
            )

    async with sessionmaker() as session2:
        async with session2.begin():
            await insert_confirmed(
                session2,
                scope=scope,
                resource_id=resource.id,
                time_range=Range(t2, t3, bounds="[)"),
            )

    async with sessionmaker() as session:
        stmt = select(func.count(BookingModel.id)).where(
            BookingModel.resource_id == resource.id,
            BookingModel.status == "confirmed",
        )
        count = (await session.execute(stmt)).scalar_one()
        assert count == 2


@pytest.mark.asyncio
async def test_04_different_resources_same_interval() -> None:
    """4. Different resources, same interval: both commit."""
    user = await create_user()
    r1 = await create_resource()
    r2 = await create_resource()
    scope = AuthorizedScope(principal_id=user.id, policy=Policy.authenticated)

    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc)

    sessionmaker = get_sessionmaker()

    async with sessionmaker() as s1:
        async with s1.begin():
            await insert_confirmed(
                s1,
                scope=scope,
                resource_id=r1.id,
                time_range=Range(t1, t2, bounds="[)"),
            )

    async with sessionmaker() as s2:
        async with s2.begin():
            await insert_confirmed(
                s2,
                scope=scope,
                resource_id=r2.id,
                time_range=Range(t1, t2, bounds="[)"),
            )

    async with sessionmaker() as session:
        stmt = select(func.count(BookingModel.id)).where(
            BookingModel.resource_id.in_([r1.id, r2.id]),
            BookingModel.status == "confirmed",
        )
        count = (await session.execute(stmt)).scalar_one()
        assert count == 2


@pytest.mark.asyncio
async def test_05_non_occupying_statuses() -> None:
    """5. Non-occupying statuses: cancelled or expired row does not block new confirmed booking."""
    user = await create_user()
    resource = await create_resource()
    scope = AuthorizedScope(principal_id=user.id, policy=Policy.authenticated)

    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    sessionmaker = get_sessionmaker()

    # Pre-insert a cancelled booking covering [10:00, 12:00)
    async with sessionmaker() as session:
        async with session.begin():
            cancelled_b = BookingModel(
                id=uuid.uuid4(),
                resource_id=resource.id,
                user_id=user.id,
                created_by=user.id,
                kind="reservation",
                time_range=Range(t1, t2, bounds="[)"),
                status="cancelled",
                cancellation_reason="user changed mind",
                version=2,
            )
            session.add(cancelled_b)

    # Now insert_confirmed for the same interval succeeds
    async with sessionmaker() as session:
        async with session.begin():
            confirmed_b = await insert_confirmed(
                session,
                scope=scope,
                resource_id=resource.id,
                time_range=Range(t1, t2, bounds="[)"),
            )
            assert confirmed_b.status == "confirmed"

    # Same check for expired booking
    t3 = datetime(2026, 7, 1, 14, 0, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 7, 1, 16, 0, 0, tzinfo=timezone.utc)
    async with sessionmaker() as session:
        async with session.begin():
            expired_b = BookingModel(
                id=uuid.uuid4(),
                resource_id=resource.id,
                user_id=user.id,
                created_by=user.id,
                kind="reservation",
                time_range=Range(t3, t4, bounds="[)"),
                status="expired",
                version=2,
            )
            session.add(expired_b)

    async with sessionmaker() as session:
        async with session.begin():
            confirmed_b2 = await insert_confirmed(
                session,
                scope=scope,
                resource_id=resource.id,
                time_range=Range(t3, t4, bounds="[)"),
            )
            assert confirmed_b2.status == "confirmed"


@pytest.mark.asyncio
async def test_06_outer_transaction_survives_conflict() -> None:
    """6. Outer transaction survives conflict: after SlotConflict, session commits another stmt."""
    user = await create_user()
    resource = await create_resource()
    scope = AuthorizedScope(principal_id=user.id, policy=Policy.authenticated)

    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 7, 1, 14, 0, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 7, 1, 16, 0, 0, tzinfo=timezone.utc)

    sessionmaker = get_sessionmaker()

    # Pre-insert confirmed booking for [10:00, 12:00)
    async with sessionmaker() as s_init:
        async with s_init.begin():
            await insert_confirmed(
                s_init,
                scope=scope,
                resource_id=resource.id,
                time_range=Range(t1, t2, bounds="[)"),
            )

    second_booking_id = None
    # In outer transaction: attempt conflict, catch SlotConflict, insert non-conflicting row
    async with sessionmaker() as session:
        async with session.begin():
            with pytest.raises(SlotConflict):
                await insert_confirmed(
                    session,
                    scope=scope,
                    resource_id=resource.id,
                    time_range=Range(t1, t2, bounds="[)"),
                )

            # The outer transaction must still be completely healthy!
            second_booking = await insert_confirmed(
                session,
                scope=scope,
                resource_id=resource.id,
                time_range=Range(t3, t4, bounds="[)"),
            )
            second_booking_id = second_booking.id

    # Verify committed state from a fresh connection
    async with sessionmaker() as fresh:
        b = await fresh.get(BookingModel, second_booking_id)
        assert b is not None
        assert b.status == "confirmed"


@pytest.mark.asyncio
async def test_07_non_conflict_integrity_errors_not_disguised() -> None:
    """7. Non-conflict integrity errors not disguised: non-23P01 errors propagate unchanged."""
    user = await create_user()
    resource = await create_resource()

    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    sessionmaker = get_sessionmaker()

    # Case A: foreign key violation on non-existent principal_id
    fake_scope = AuthorizedScope(principal_id=uuid.uuid4(), policy=Policy.authenticated)
    async with sessionmaker() as session:
        async with session.begin():
            with pytest.raises(IntegrityError) as exc_info:
                await insert_confirmed(
                    session,
                    scope=fake_scope,
                    resource_id=resource.id,
                    time_range=Range(t1, t2, bounds="[)"),
                )
            assert not isinstance(exc_info.value, SlotConflict)
            orig = getattr(exc_info.value, "orig", None)
            driver_exc = getattr(orig, "__cause__", None) or orig
            assert getattr(driver_exc, "sqlstate", None) == "23503"  # foreign_key_violation

    # Case B: check constraint violation (e.g. invalid bounds on time_range)
    scope = AuthorizedScope(principal_id=user.id, policy=Policy.authenticated)
    async with sessionmaker() as session:
        async with session.begin():
            with pytest.raises(IntegrityError) as exc_info:
                await insert_confirmed(
                    session,
                    scope=scope,
                    resource_id=resource.id,
                    time_range=Range(t1, t2, bounds="[]"),
                )
            assert not isinstance(exc_info.value, SlotConflict)
            orig = getattr(exc_info.value, "orig", None)
            driver_exc = getattr(orig, "__cause__", None) or orig
            assert getattr(driver_exc, "sqlstate", None) == "23514"  # check_violation
            c_name = getattr(driver_exc, "constraint_name", None)
            assert c_name == "bookings_time_range_check"
            assert c_name != "bookings_no_overlap"


@pytest.mark.asyncio
async def test_08_atomic_rollback_of_booking_and_event() -> None:
    """8. Atomic rollback of booking and event together: transaction rollback rolls both back."""
    user = await create_user()
    resource = await create_resource()
    scope = AuthorizedScope(principal_id=user.id, policy=Policy.authenticated)

    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    sessionmaker = get_sessionmaker()

    booking_id = None
    event_id = None

    async with sessionmaker() as session:
        async with session.begin() as trans:
            b = await insert_confirmed(
                session,
                scope=scope,
                resource_id=resource.id,
                time_range=Range(t1, t2, bounds="[)"),
            )
            booking_id = b.id
            event_id = await append_event(
                session,
                event_type="booking_confirmed",
                booking=b,
            )
            # Explicit rollback
            await trans.rollback()

    # Assert from a fresh connection that neither row exists
    async with sessionmaker() as fresh:
        b_count = (
            await fresh.execute(
                select(func.count(BookingModel.id)).where(BookingModel.id == booking_id)
            )
        ).scalar_one()
        ev_count = (
            await fresh.execute(select(func.count(Outbox.id)).where(Outbox.id == event_id))
        ).scalar_one()
        assert b_count == 0
        assert ev_count == 0


@pytest.mark.asyncio
async def test_09_event_shape() -> None:
    """9. Event shape: exact 7 payload keys, post-change version, no PII, pending status."""
    user = await create_user(email="sensitive_owner@example.com")
    resource = await create_resource(name="Secret Conference Room")
    scope = AuthorizedScope(principal_id=user.id, policy=Policy.authenticated)

    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 7, 1, 9, 30, 0, tzinfo=timezone.utc)

    sessionmaker = get_sessionmaker()

    event_id = None
    booking_id = None
    async with sessionmaker() as session:
        async with session.begin():
            b = await insert_confirmed(
                session,
                scope=scope,
                resource_id=resource.id,
                time_range=Range(t1, t2, bounds="[)"),
                now=now,
            )
            booking_id = b.id
            event_id = await append_event(
                session,
                event_type="booking_confirmed",
                booking=b,
                now=now,
            )

    async with sessionmaker() as fresh:
        ev = await fresh.get(Outbox, event_id)
        assert ev is not None
        assert ev.aggregate_id == booking_id
        assert ev.aggregate_version == 1
        assert ev.event_type == "booking_confirmed"
        assert ev.status == "pending"
        assert ev.lease_until is None
        assert ev.lease_token is None

        payload = ev.payload
        expected_keys = {
            "schema_version",
            "booking_id",
            "recipient_id",
            "resource_id",
            "starts_at",
            "ends_at",
            "expires_at",
        }
        assert set(payload.keys()) == expected_keys
        assert payload["schema_version"] == 1
        assert payload["booking_id"] == str(booking_id)
        assert payload["recipient_id"] == str(user.id)
        assert payload["resource_id"] == str(resource.id)
        assert payload["starts_at"] == "2026-07-01T10:00:00.000000Z"
        assert payload["ends_at"] == "2026-07-01T12:00:00.000000Z"
        assert payload["expires_at"] is None

        # Assert no email, no resource name, no free text anywhere in payload
        payload_str = str(payload)
        assert "sensitive_owner@example.com" not in payload_str
        assert "Secret Conference Room" not in payload_str


@pytest.mark.asyncio
async def test_10_no_event_for_blackout() -> None:
    """10. No event for blackout: confirmed blackout leaves outbox table empty for aggregate."""
    admin = await create_user(role="admin")
    resource = await create_resource()
    admin_scope = AuthorizedScope(principal_id=admin.id, policy=Policy.admin)

    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    sessionmaker = get_sessionmaker()

    blackout_id = None
    async with sessionmaker() as session:
        async with session.begin():
            b = await insert_confirmed(
                session,
                scope=admin_scope,
                resource_id=resource.id,
                time_range=Range(t1, t2, bounds="[)"),
                kind="blackout",
            )
            blackout_id = b.id
            # Caller does not call append_event for blackout per DECISION I

    async with sessionmaker() as fresh:
        stmt = select(func.count(Outbox.id)).where(Outbox.aggregate_id == blackout_id)
        count = (await fresh.execute(stmt)).scalar_one()
        assert count == 0


@pytest.mark.asyncio
async def test_11_lock_mode_is_for_share() -> None:
    """11. Lock mode is FOR SHARE: resource lock allows concurrent non-overlapping inserts."""
    user = await create_user()
    resource = await create_resource()
    scope = AuthorizedScope(principal_id=user.id, policy=Policy.authenticated)

    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 7, 1, 14, 0, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 7, 1, 15, 0, 0, tzinfo=timezone.utc)

    sessionmaker = get_sessionmaker()

    t1_locked = asyncio.Event()
    t2_finished = asyncio.Event()

    async def tx1() -> None:
        try:
            async with sessionmaker() as s1:
                async with s1.begin():
                    await insert_confirmed(
                        s1,
                        scope=scope,
                        resource_id=resource.id,
                        time_range=Range(t1, t2, bounds="[)"),
                    )
                    t1_locked.set()
                    # Wait for tx2 to complete its insert and commit while tx1 is uncommitted
                    await t2_finished.wait()
        finally:
            t1_locked.set()

    async def tx2() -> None:
        await t1_locked.wait()
        async with sessionmaker() as s2:
            async with s2.begin():
                # If resource lock is FOR SHARE, this does not block on tx1
                await insert_confirmed(
                    s2,
                    scope=scope,
                    resource_id=resource.id,
                    time_range=Range(t3, t4, bounds="[)"),
                )
        t2_finished.set()

    # If lock mode were FOR UPDATE, tx2 would block on tx1, deadlocking tx1 waiting for t2_finished.
    # With FOR SHARE, both complete cleanly within timeout.
    await asyncio.wait_for(asyncio.gather(tx1(), tx2()), timeout=5.0)

    async with sessionmaker() as fresh:
        stmt = select(func.count(BookingModel.id)).where(
            BookingModel.resource_id == resource.id,
            BookingModel.status == "confirmed",
        )
        count = (await fresh.execute(stmt)).scalar_one()
        assert count == 2


@pytest.mark.asyncio
async def test_12_resource_validation_not_found_and_inactive() -> None:
    """Resource validation: NotFoundError for missing resource, ResourceInactive for inactive."""
    user = await create_user()
    scope = AuthorizedScope(principal_id=user.id, policy=Policy.authenticated)
    sessionmaker = get_sessionmaker()
    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc)

    # Missing resource
    missing_id = uuid.uuid4()
    async with sessionmaker() as session:
        async with session.begin():
            with pytest.raises(NotFoundError):
                await insert_confirmed(
                    session,
                    scope=scope,
                    resource_id=missing_id,
                    time_range=Range(t1, t2, bounds="[)"),
                )

    # Inactive resource
    inactive_res = await create_resource(active=False)
    async with sessionmaker() as session:
        async with session.begin():
            with pytest.raises(ResourceInactive):
                await insert_confirmed(
                    session,
                    scope=scope,
                    resource_id=inactive_res.id,
                    time_range=Range(t1, t2, bounds="[)"),
                )


@pytest.mark.asyncio
async def test_13_acting_identity_derived_from_authorized_scope() -> None:
    """13. Acting identity is derived strictly from scope.principal_id."""
    user_x = await create_user()
    user_y = await create_user()
    admin = await create_user(role="admin")
    resource = await create_resource()

    t1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 7, 1, 13, 0, 0, tzinfo=timezone.utc)

    sessionmaker = get_sessionmaker()

    # Reservation with user_x scope: user_id and created_by must equal user_x.id, not user_y.id
    scope_x = AuthorizedScope(principal_id=user_x.id, policy=Policy.authenticated)
    async with sessionmaker() as session:
        async with session.begin():
            booking = await insert_confirmed(
                session,
                scope=scope_x,
                resource_id=resource.id,
                time_range=Range(t1, t2, bounds="[)"),
                kind="reservation",
            )
            assert booking.user_id == user_x.id
            assert booking.created_by == user_x.id
            assert booking.user_id != user_y.id
            assert booking.created_by != user_y.id
            assert booking.status == "confirmed"

    # Blackout with admin scope: user_id must be None and created_by must equal admin.id
    admin_scope = AuthorizedScope(principal_id=admin.id, policy=Policy.admin)
    async with sessionmaker() as session:
        async with session.begin():
            blackout = await insert_confirmed(
                session,
                scope=admin_scope,
                resource_id=resource.id,
                time_range=Range(t3, t4, bounds="[)"),
                kind="blackout",
            )
            assert blackout.user_id is None
            assert blackout.created_by == admin.id
            assert blackout.created_by != user_x.id
            assert blackout.created_by != user_y.id
            assert blackout.status == "confirmed"
