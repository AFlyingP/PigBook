import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from alembic.config import Config
from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app.admin.models  # noqa: F401
import app.auth.models  # noqa: F401
import app.bookings.models  # noqa: F401
import app.notifications.models  # noqa: F401
import app.resources.models  # noqa: F401
import app.waitlist.models  # noqa: F401
from alembic import command
from app.auth.models import User
from app.bookings.models import Booking
from app.db.base import Base
from app.resources.models import Resource

EXPECTED_TABLES = {
    "users",
    "invitations",
    "refresh_tokens",
    "resources",
    "bookings",
    "waitlist_entries",
    "idempotency_keys",
    "outbox",
    "notification_deliveries",
    "audit_log",
    "feedback",
    "rate_limits",
    "worker_heartbeat",
}


@pytest.mark.asyncio
async def test_08a_migration_lifecycle(db_engine: AsyncEngine) -> None:
    """8a. Migrating from empty to head creates all 13 tables;

    downgrade to base removes them; upgrade to head again succeeds.
    """
    cfg = Config("backend/alembic.ini")

    # 1. Upgrade to head
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with db_engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
        tables = {row[0] for row in res.fetchall()}
        assert EXPECTED_TABLES.issubset(tables), (
            f"Missing tables after upgrade: {EXPECTED_TABLES - tables}"
        )

    # 2. Downgrade to base
    await asyncio.to_thread(command.downgrade, cfg, "base")

    async with db_engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
        tables = {row[0] for row in res.fetchall()}
        for t in EXPECTED_TABLES:
            assert t not in tables, f"Table {t} was not removed after downgrade"

    # 3. Upgrade to head again
    await asyncio.to_thread(command.upgrade, cfg, "head")

    async with db_engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
        tables = {row[0] for row in res.fetchall()}
        assert EXPECTED_TABLES.issubset(tables), (
            f"Missing tables after second upgrade: {EXPECTED_TABLES - tables}"
        )


@pytest.mark.asyncio
async def test_08b_exclusion_constraint_definition(db_session: AsyncSession) -> None:
    """8b. Inspect pg_constraint: bookings_no_overlap exists, is contype 'x',

    is NOT deferrable, with resource_id =, time_range &&, and status predicate.
    """
    query = text(
        "SELECT conname, contype, condeferrable, pg_get_constraintdef(oid) AS condef "
        "FROM pg_constraint "
        "WHERE conname = 'bookings_no_overlap'"
    )
    res = await db_session.execute(query)
    row = res.mappings().first()
    assert row is not None, "bookings_no_overlap constraint not found in pg_constraint"
    assert row["conname"] == "bookings_no_overlap"
    contype = row["contype"].decode() if isinstance(row["contype"], bytes) else row["contype"]
    assert contype == "x", f"Expected exclusion constraint contype 'x', got '{contype}'"
    assert row["condeferrable"] is False, "bookings_no_overlap must NOT be deferrable"

    condef = row["condef"]
    assert "resource_id" in condef and "=" in condef, f"Missing resource_id with = in {condef}"
    assert "time_range" in condef and "&&" in condef, f"Missing time_range with && in {condef}"
    assert "confirmed" in condef and "offered" in condef, (
        f"Missing confirmed/offered in predicate {condef}"
    )


@pytest.mark.asyncio
async def test_08c_overlapping_confirmed_bookings_rejected(db_session: AsyncSession) -> None:
    """8c. Two overlapping confirmed bookings on one resource:

    second INSERT raises SQLSTATE 23P01 naming bookings_no_overlap.
    """
    user_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    await db_session.execute(
        insert(User).values(
            id=user_id,
            email="user8c@example.com",
            password_hash="hashed",
            display_name="User 8c",
        )
    )
    await db_session.execute(
        insert(Resource).values(
            id=resource_id,
            name="Resource 8c",
            location="Room 8c",
        )
    )

    t1 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc)
    t4 = datetime(2026, 10, 1, 11, 30, tzinfo=timezone.utc)

    # First booking succeeds
    await db_session.execute(
        insert(Booking).values(
            id=uuid.uuid4(),
            resource_id=resource_id,
            user_id=user_id,
            created_by=user_id,
            kind="reservation",
            time_range=Range(t1, t2, bounds="[)"),
            status="confirmed",
        )
    )
    await db_session.flush()

    # Second overlapping booking must raise SQLSTATE 23P01 naming bookings_no_overlap
    with pytest.raises(IntegrityError) as exc_info:
        async with db_session.begin_nested():
            await db_session.execute(
                insert(Booking).values(
                    id=uuid.uuid4(),
                    resource_id=resource_id,
                    user_id=user_id,
                    created_by=user_id,
                    kind="reservation",
                    time_range=Range(t3, t4, bounds="[)"),
                    status="confirmed",
                )
            )
            await db_session.flush()

    err_str = str(exc_info.value)
    assert "bookings_no_overlap" in err_str, f"Expected 'bookings_no_overlap' in error: {err_str}"
    orig = getattr(exc_info.value, "orig", None)
    pgcode = getattr(orig, "sqlstate", None) or getattr(
        getattr(orig, "__cause__", None), "sqlstate", None
    )
    assert pgcode == "23P01" or "23P01" in err_str, (
        f"Expected SQLSTATE 23P01, got {pgcode} in {err_str}"
    )


@pytest.mark.asyncio
async def test_08d_adjacent_intervals_both_commit(db_session: AsyncSession) -> None:
    """8d. Adjacent intervals ([09:00,10:00) then [10:00,11:00)) both commit."""
    user_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    await db_session.execute(
        insert(User).values(
            id=user_id,
            email="user8d@example.com",
            password_hash="hashed",
            display_name="User 8d",
        )
    )
    await db_session.execute(
        insert(Resource).values(
            id=resource_id,
            name="Resource 8d",
            location="Room 8d",
        )
    )

    t1 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc)

    await db_session.execute(
        insert(Booking).values(
            id=uuid.uuid4(),
            resource_id=resource_id,
            user_id=user_id,
            created_by=user_id,
            kind="reservation",
            time_range=Range(t1, t2, bounds="[)"),
            status="confirmed",
        )
    )
    await db_session.execute(
        insert(Booking).values(
            id=uuid.uuid4(),
            resource_id=resource_id,
            user_id=user_id,
            created_by=user_id,
            kind="reservation",
            time_range=Range(t2, t3, bounds="[)"),
            status="confirmed",
        )
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_08e_same_interval_different_resources(db_session: AsyncSession) -> None:
    """8e. Same interval on two different resources both commit."""
    user_id = uuid.uuid4()
    res1_id = uuid.uuid4()
    res2_id = uuid.uuid4()
    await db_session.execute(
        insert(User).values(
            id=user_id,
            email="user8e@example.com",
            password_hash="hashed",
            display_name="User 8e",
        )
    )
    await db_session.execute(
        insert(Resource).values(
            id=res1_id,
            name="Resource 8e-1",
            location="Room 8e-1",
        )
    )
    await db_session.execute(
        insert(Resource).values(
            id=res2_id,
            name="Resource 8e-2",
            location="Room 8e-2",
        )
    )

    t1 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)

    await db_session.execute(
        insert(Booking).values(
            id=uuid.uuid4(),
            resource_id=res1_id,
            user_id=user_id,
            created_by=user_id,
            kind="reservation",
            time_range=Range(t1, t2, bounds="[)"),
            status="confirmed",
        )
    )
    await db_session.execute(
        insert(Booking).values(
            id=uuid.uuid4(),
            resource_id=res2_id,
            user_id=user_id,
            created_by=user_id,
            kind="reservation",
            time_range=Range(t1, t2, bounds="[)"),
            status="confirmed",
        )
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_08f_inactive_status_and_offered_overlap(db_session: AsyncSession) -> None:
    """8f. Overlap with cancelled or expired commits; overlap against offered is rejected."""
    user_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    await db_session.execute(
        insert(User).values(
            id=user_id,
            email="user8f@example.com",
            password_hash="hashed",
            display_name="User 8f",
        )
    )
    await db_session.execute(
        insert(Resource).values(
            id=resource_id,
            name="Resource 8f",
            location="Room 8f",
        )
    )

    # 1. Overlap against 'cancelled' commits
    t1 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
    await db_session.execute(
        insert(Booking).values(
            id=uuid.uuid4(),
            resource_id=resource_id,
            user_id=user_id,
            created_by=user_id,
            kind="reservation",
            time_range=Range(t1, t2, bounds="[)"),
            status="cancelled",
        )
    )
    t_over1 = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)
    t_over2 = datetime(2026, 10, 1, 15, 30, tzinfo=timezone.utc)
    await db_session.execute(
        insert(Booking).values(
            id=uuid.uuid4(),
            resource_id=resource_id,
            user_id=user_id,
            created_by=user_id,
            kind="reservation",
            time_range=Range(t_over1, t_over2, bounds="[)"),
            status="confirmed",
        )
    )
    await db_session.flush()

    # 2. Overlap against 'expired' commits
    t3 = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
    await db_session.execute(
        insert(Booking).values(
            id=uuid.uuid4(),
            resource_id=resource_id,
            user_id=user_id,
            created_by=user_id,
            kind="reservation",
            time_range=Range(t3, t4, bounds="[)"),
            status="expired",
        )
    )
    t_over3 = datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc)
    t_over4 = datetime(2026, 10, 1, 17, 30, tzinfo=timezone.utc)
    await db_session.execute(
        insert(Booking).values(
            id=uuid.uuid4(),
            resource_id=resource_id,
            user_id=user_id,
            created_by=user_id,
            kind="reservation",
            time_range=Range(t_over3, t_over4, bounds="[)"),
            status="confirmed",
        )
    )
    await db_session.flush()

    # 3. Overlap against 'offered' is rejected
    t5 = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
    t6 = datetime(2026, 10, 1, 19, 0, tzinfo=timezone.utc)
    t_exp = datetime(2026, 10, 1, 17, 30, tzinfo=timezone.utc)
    await db_session.execute(
        insert(Booking).values(
            id=uuid.uuid4(),
            resource_id=resource_id,
            user_id=user_id,
            created_by=user_id,
            kind="reservation",
            time_range=Range(t5, t6, bounds="[)"),
            status="offered",
            expires_at=t_exp,
        )
    )
    await db_session.flush()

    t_over5 = datetime(2026, 10, 1, 18, 30, tzinfo=timezone.utc)
    t_over6 = datetime(2026, 10, 1, 19, 30, tzinfo=timezone.utc)
    with pytest.raises(IntegrityError) as exc_info:
        async with db_session.begin_nested():
            await db_session.execute(
                insert(Booking).values(
                    id=uuid.uuid4(),
                    resource_id=resource_id,
                    user_id=user_id,
                    created_by=user_id,
                    kind="reservation",
                    time_range=Range(t_over5, t_over6, bounds="[)"),
                    status="confirmed",
                )
            )
            await db_session.flush()
    assert "bookings_no_overlap" in str(exc_info.value)


@pytest.mark.asyncio
async def test_08g_blackout_conflicts_with_confirmed_reservation(db_session: AsyncSession) -> None:
    """8g. A blackout (kind='blackout', user_id NULL) conflicts with confirmed reservation."""
    admin_id = uuid.uuid4()
    member_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    await db_session.execute(
        insert(User).values(
            id=admin_id,
            email="admin8g@example.com",
            password_hash="hashed",
            display_name="Admin 8g",
            role="admin",
        )
    )
    await db_session.execute(
        insert(User).values(
            id=member_id,
            email="member8g@example.com",
            password_hash="hashed",
            display_name="Member 8g",
            role="member",
        )
    )
    await db_session.execute(
        insert(Resource).values(
            id=resource_id,
            name="Resource 8g",
            location="Room 8g",
        )
    )

    t1 = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

    # Blackout row
    await db_session.execute(
        insert(Booking).values(
            id=uuid.uuid4(),
            resource_id=resource_id,
            user_id=None,
            created_by=admin_id,
            kind="blackout",
            time_range=Range(t1, t2, bounds="[)"),
            status="confirmed",
        )
    )
    await db_session.flush()

    # Confirmed reservation overlapping blackout window must raise 23P01
    t_sub1 = datetime(2026, 10, 2, 10, 30, tzinfo=timezone.utc)
    t_sub2 = datetime(2026, 10, 2, 11, 30, tzinfo=timezone.utc)
    with pytest.raises(IntegrityError) as exc_info:
        async with db_session.begin_nested():
            await db_session.execute(
                insert(Booking).values(
                    id=uuid.uuid4(),
                    resource_id=resource_id,
                    user_id=member_id,
                    created_by=member_id,
                    kind="reservation",
                    time_range=Range(t_sub1, t_sub2, bounds="[)"),
                    status="confirmed",
                )
            )
            await db_session.flush()
    assert "bookings_no_overlap" in str(exc_info.value)


@pytest.mark.asyncio
async def test_08h_check_constraints_enforced(db_session: AsyncSession) -> None:
    """8h. CHECK enforcement:

    - reservation with NULL user_id rejected
    - blackout with non-null user_id rejected
    - blackout with status 'offered' rejected
    - empty or unbounded time_range rejected
    - 'offered' row without expires_at rejected
    - version <= 0 rejected
    - non-normalized / overlong email rejected
    """
    user_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    resource_id = uuid.uuid4()

    await db_session.execute(
        insert(User).values(
            id=user_id,
            email="user8h@example.com",
            password_hash="hashed",
            display_name="User 8h",
        )
    )
    await db_session.execute(
        insert(User).values(
            id=admin_id,
            email="admin8h@example.com",
            password_hash="hashed",
            display_name="Admin 8h",
            role="admin",
        )
    )
    await db_session.execute(
        insert(Resource).values(
            id=resource_id,
            name="Resource 8h",
            location="Room 8h",
        )
    )
    await db_session.flush()

    t1 = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 3, 11, 0, tzinfo=timezone.utc)

    # 1. Reservation with NULL user_id is rejected
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(Booking).values(
                    id=uuid.uuid4(),
                    resource_id=resource_id,
                    user_id=None,
                    created_by=user_id,
                    kind="reservation",
                    time_range=Range(t1, t2, bounds="[)"),
                    status="confirmed",
                )
            )
            await db_session.flush()

    # 2. Blackout with non-null user_id is rejected
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(Booking).values(
                    id=uuid.uuid4(),
                    resource_id=resource_id,
                    user_id=user_id,
                    created_by=admin_id,
                    kind="blackout",
                    time_range=Range(t1, t2, bounds="[)"),
                    status="confirmed",
                )
            )
            await db_session.flush()

    # 3. Blackout with status 'offered' is rejected
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(Booking).values(
                    id=uuid.uuid4(),
                    resource_id=resource_id,
                    user_id=None,
                    created_by=admin_id,
                    kind="blackout",
                    time_range=Range(t1, t2, bounds="[)"),
                    status="offered",
                    expires_at=datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc),
                )
            )
            await db_session.flush()

    # 4. Empty or unbounded time_range is rejected
    with pytest.raises((IntegrityError, DBAPIError)):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(Booking).values(
                    id=uuid.uuid4(),
                    resource_id=resource_id,
                    user_id=user_id,
                    created_by=user_id,
                    kind="reservation",
                    time_range=Range(t1, t1, bounds="[)"),  # empty
                    status="confirmed",
                )
            )
            await db_session.flush()

    with pytest.raises((IntegrityError, DBAPIError)):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(Booking).values(
                    id=uuid.uuid4(),
                    resource_id=resource_id,
                    user_id=user_id,
                    created_by=user_id,
                    kind="reservation",
                    time_range=Range(t1, None, bounds="[)"),  # upper unbounded
                    status="confirmed",
                )
            )
            await db_session.flush()

    # 5. 'offered' row without expires_at is rejected
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(Booking).values(
                    id=uuid.uuid4(),
                    resource_id=resource_id,
                    user_id=user_id,
                    created_by=user_id,
                    kind="reservation",
                    time_range=Range(t1, t2, bounds="[)"),
                    status="offered",
                    expires_at=None,
                )
            )
            await db_session.flush()

    # 6. version <= 0 is rejected
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(User).values(
                    id=uuid.uuid4(),
                    email="zeroversion@example.com",
                    password_hash="hashed",
                    display_name="Zero Version",
                    version=0,
                )
            )
            await db_session.flush()

    # 7. Non-normalized or overlong email is rejected
    # Non-normalized: uppercase
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(User).values(
                    id=uuid.uuid4(),
                    email="UpperCase@example.com",
                    password_hash="hashed",
                    display_name="Upper",
                )
            )
            await db_session.flush()

    # Non-normalized: leading whitespace
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(User).values(
                    id=uuid.uuid4(),
                    email=" space@example.com",
                    password_hash="hashed",
                    display_name="Space",
                )
            )
            await db_session.flush()

    # Overlong email (> 254 chars)
    overlong = ("a" * 245) + "@example.com"
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(User).values(
                    id=uuid.uuid4(),
                    email=overlong,
                    password_hash="hashed",
                    display_name="Overlong",
                )
            )
            await db_session.flush()


@pytest.mark.asyncio
async def test_08i_trigger_enforcement(db_session: AsyncSession) -> None:
    """8i. Trigger enforcement:

    - UPDATE changing resource_id, user_id, kind, time_range or created_by raises
    - illegal status transition (e.g. cancelled -> confirmed) raises
    - UPDATE not incrementing version by exactly 1 raises
    - legal transition (confirmed -> cancelled) with version+1 succeeds
    """
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    res_a = uuid.uuid4()
    res_b = uuid.uuid4()

    await db_session.execute(
        insert(User).values(
            id=user_a,
            email="usera8i@example.com",
            password_hash="hashed",
            display_name="User A 8i",
        )
    )
    await db_session.execute(
        insert(User).values(
            id=user_b,
            email="userb8i@example.com",
            password_hash="hashed",
            display_name="User B 8i",
        )
    )
    await db_session.execute(
        insert(Resource).values(
            id=res_a,
            name="Resource A 8i",
            location="Room A",
        )
    )
    await db_session.execute(
        insert(Resource).values(
            id=res_b,
            name="Resource B 8i",
            location="Room B",
        )
    )

    t1 = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 4, 11, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc)

    booking_id = uuid.uuid4()
    await db_session.execute(
        insert(Booking).values(
            id=booking_id,
            resource_id=res_a,
            user_id=user_a,
            created_by=user_a,
            kind="reservation",
            time_range=Range(t1, t2, bounds="[)"),
            status="confirmed",
            version=1,
        )
    )
    await db_session.flush()

    # 1. Changing resource_id raises
    with pytest.raises(IntegrityError) as exc_info:
        async with db_session.begin_nested():
            await db_session.execute(
                update(Booking).where(Booking.id == booking_id).values(resource_id=res_b, version=2)
            )
            await db_session.flush()
    assert "immutable booking identity/window" in str(exc_info.value)

    # 2. Changing user_id raises
    with pytest.raises(IntegrityError) as exc_info:
        async with db_session.begin_nested():
            await db_session.execute(
                update(Booking).where(Booking.id == booking_id).values(user_id=user_b, version=2)
            )
            await db_session.flush()
    assert "immutable booking identity/window" in str(exc_info.value)

    # 3. Changing kind raises
    with pytest.raises(IntegrityError) as exc_info:
        async with db_session.begin_nested():
            await db_session.execute(
                update(Booking).where(Booking.id == booking_id).values(kind="blackout", version=2)
            )
            await db_session.flush()
    assert "immutable booking identity/window" in str(exc_info.value)

    # 4. Changing time_range raises
    with pytest.raises(IntegrityError) as exc_info:
        async with db_session.begin_nested():
            await db_session.execute(
                update(Booking)
                .where(Booking.id == booking_id)
                .values(time_range=Range(t3, t4, bounds="[)"), version=2)
            )
            await db_session.flush()
    assert "immutable booking identity/window" in str(exc_info.value)

    # 5. Changing created_by raises
    with pytest.raises(IntegrityError) as exc_info:
        async with db_session.begin_nested():
            await db_session.execute(
                update(Booking).where(Booking.id == booking_id).values(created_by=user_b, version=2)
            )
            await db_session.flush()
    assert "immutable booking identity/window" in str(exc_info.value)

    # 6. Illegal status transition (e.g. cancelled -> confirmed)
    # First create a cancelled booking
    cancelled_id = uuid.uuid4()
    await db_session.execute(
        insert(Booking).values(
            id=cancelled_id,
            resource_id=res_a,
            user_id=user_a,
            created_by=user_a,
            kind="reservation",
            time_range=Range(t3, t4, bounds="[)"),
            status="cancelled",
            version=1,
        )
    )
    await db_session.flush()

    with pytest.raises(IntegrityError) as exc_info:
        async with db_session.begin_nested():
            await db_session.execute(
                update(Booking)
                .where(Booking.id == cancelled_id)
                .values(status="confirmed", version=2)
            )
            await db_session.flush()
    assert "invalid booking transition" in str(exc_info.value)

    # 7. UPDATE that does not increment version by exactly 1 raises
    with pytest.raises(IntegrityError) as exc_info:
        async with db_session.begin_nested():
            await db_session.execute(
                update(Booking)
                .where(Booking.id == booking_id)
                .values(status="cancelled", version=1)  # version unchanged
            )
            await db_session.flush()
    assert "version must increment" in str(exc_info.value)

    with pytest.raises(IntegrityError) as exc_info:
        async with db_session.begin_nested():
            await db_session.execute(
                update(Booking)
                .where(Booking.id == booking_id)
                .values(status="cancelled", version=3)  # version incremented by 2
            )
            await db_session.flush()
    assert "version must increment" in str(exc_info.value)

    # 8. Legal transition (confirmed -> cancelled) with version+1 succeeds
    await db_session.execute(
        update(Booking).where(Booking.id == booking_id).values(status="cancelled", version=2)
    )
    await db_session.flush()

    res = await db_session.execute(
        select(Booking.status, Booking.version).where(Booking.id == booking_id)
    )
    row = res.first()
    assert row is not None
    assert row.status == "cancelled"
    assert row.version == 2


@pytest.mark.asyncio
async def test_08j_metadata_agrees_with_live_database(db_session: AsyncSession) -> None:
    """8j. Model metadata agrees with migrated database:

    for all 13 tables compare table names and, per table, column names and nullability.
    """
    metadata_tables = Base.metadata.tables

    # Check table names
    for table_name in EXPECTED_TABLES:
        assert table_name in metadata_tables, f"Table {table_name} missing from Base.metadata"

    for table_name in EXPECTED_TABLES:
        orm_table = metadata_tables[table_name]
        orm_columns = {col.name: col for col in orm_table.columns}

        # Query information_schema for this table
        res = await db_session.execute(
            text(
                "SELECT column_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :tbl"
            ),
            {"tbl": table_name},
        )
        db_columns = {row[0]: row[1] for row in res.fetchall()}
        assert db_columns, f"No columns found in database for table {table_name}"

        # Compare column names
        assert set(orm_columns.keys()) == set(db_columns.keys()), (
            f"Column set mismatch for table '{table_name}': "
            f"ORM has {set(orm_columns.keys())}, DB has {set(db_columns.keys())}"
        )

        # Compare column nullability
        for col_name, orm_col in orm_columns.items():
            expected_nullable = "YES" if orm_col.nullable else "NO"
            actual_nullable = db_columns[col_name]
            assert actual_nullable == expected_nullable, (
                f"Nullability mismatch on {table_name}.{col_name}: "
                f"ORM expects {expected_nullable}, DB is {actual_nullable}"
            )


@pytest.mark.asyncio
async def test_session_settings_and_generator() -> None:
    """Verify session.py exports get_session and transaction_dependency

    with UTC timezone and timeouts configured.
    """
    from app.db.session import get_session, transaction_dependency

    async for session in get_session():
        tz = (await session.execute(text("SHOW TimeZone"))).scalar()
        lock_to = (await session.execute(text("SHOW lock_timeout"))).scalar()
        stmt_to = (await session.execute(text("SHOW statement_timeout"))).scalar()
        idle_to = (await session.execute(text("SHOW idle_in_transaction_session_timeout"))).scalar()

        assert tz == "UTC", f"Expected TimeZone UTC, got {tz}"
        assert lock_to == "15s", f"Expected lock_timeout 15s, got {lock_to}"
        assert stmt_to == "30s", f"Expected statement_timeout 30s, got {stmt_to}"
        assert idle_to == "30s", f"Expected idle_in_transaction_session_timeout 30s, got {idle_to}"

    async for session in transaction_dependency():
        tz = (await session.execute(text("SHOW TimeZone"))).scalar()
        assert tz == "UTC"
