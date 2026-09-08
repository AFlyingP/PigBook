"""Integration tests for waitlist offer acceptance and withdrawal (Phase C / legacy T-016)."""

import asyncio
import os
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

from app.auth.models import User
from app.auth.passwords import hash_password
from app.bookings.models import Booking
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app
from app.notifications.models import Outbox
from app.resources.models import Resource
from app.waitlist.models import WaitlistEntry

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
    email = f"act_{uuid.uuid4().hex[:8]}@example.com"
    pwd_hash = hash_password("valid-password-123")
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=pwd_hash,
        display_name="Action User",
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
        name=f"ActResource_{uuid.uuid4().hex[:6]}",
        description="Action Test Resource",
        location="Studio C",
        active=active,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(resource)
    return resource


def aligned_slot(
    days: int = 5, hour: int = 10, duration_hours: int = 1
) -> tuple[datetime, datetime]:
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(
        days=days
    )
    start = base.replace(hour=hour)
    end = start + timedelta(hours=duration_hours)
    return start, end


async def create_offered_pair(
    *,
    resource: Resource,
    user: User,
    starts_at: datetime,
    ends_at: datetime,
    expires_at: datetime,
    entry_version: int = 1,
    booking_version: int = 1,
) -> tuple[WaitlistEntry, Booking]:
    now = datetime.now(timezone.utc)
    booking = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=user.id,
        created_by=user.id,
        kind="reservation",
        time_range=Range(starts_at, ends_at, bounds="[)"),
        status="offered",
        expires_at=expires_at,
        version=booking_version,
        created_at=now,
        updated_at=now,
    )
    entry = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=user.id,
        resource_id=resource.id,
        time_range=Range(starts_at, ends_at, bounds="[)"),
        status="offered",
        offered_booking_id=booking.id,
        version=entry_version,
        created_at=now,
        updated_at=now,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(booking)
            session.add(entry)
    return entry, booking


# --- Tests ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_offer_success_and_expiry_at_or_after_deadline() -> None:
    """Acceptance before deadline succeeds; acceptance at or after deadline returns 409

    HOLD_EXPIRED with expiration persisted.
    """
    resource = await create_resource()
    user_succ = await create_user()
    user_exp = await create_user()
    next_waiter = await create_user()

    s1, e1 = aligned_slot(days=5, hour=10)
    now = datetime.now(timezone.utc)

    # 1. Successful acceptance before deadline
    entry_succ, booking_succ = await create_offered_pair(
        resource=resource,
        user=user_succ,
        starts_at=s1,
        ends_at=e1,
        expires_at=now + timedelta(minutes=10),
    )

    async with make_client() as client:
        r_accept = await client.post(
            f"/api/v1/waitlist/{entry_succ.id}/accept",
            json={},
            headers={**auth(user_succ), "If-Match": '"1"'},
        )
        assert r_accept.status_code == 200, r_accept.text
        data = r_accept.json()
        assert data["id"] == str(booking_succ.id)
        assert data["status"] == "confirmed"
        assert data["version"] == 2
        assert r_accept.headers.get("etag") == '"2"'

    # Verify single confirmed booking and booking_confirmed outbox event
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        b_row = (
            await session.execute(select(Booking).where(Booking.id == booking_succ.id))
        ).scalar_one()
        assert b_row.status == "confirmed"
        assert b_row.expires_at is None
        assert b_row.version == 2

        e_row = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_succ.id))
        ).scalar_one()
        assert e_row.status == "accepted"
        assert e_row.version == 2

        events = (
            (
                await session.execute(
                    select(Outbox).where(
                        Outbox.aggregate_id == booking_succ.id,
                        Outbox.event_type == "booking_confirmed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1

    # 2. Acceptance at or after deadline returns 409 HOLD_EXPIRED with expiration persisted
    # and promotes next eligible waiter
    s2, e2 = aligned_slot(days=5, hour=14)
    # Offered pair with expired deadline (e.g. 5 seconds in the past)
    entry_exp, booking_exp = await create_offered_pair(
        resource=resource,
        user=user_exp,
        starts_at=s2,
        ends_at=e2,
        expires_at=now - timedelta(seconds=5),
    )

    # Next waiter waiting for the exact same window
    entry_next = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=next_waiter.id,
        resource_id=resource.id,
        time_range=Range(s2, e2, bounds="[)"),
        status="waiting",
        version=1,
        created_at=now + timedelta(seconds=1),
        updated_at=now + timedelta(seconds=1),
    )
    async with sessionmaker() as session:
        async with session.begin():
            session.add(entry_next)

    async with make_client() as client:
        r_exp = await client.post(
            f"/api/v1/waitlist/{entry_exp.id}/accept",
            json={},
            headers={**auth(user_exp), "If-Match": '"1"'},
        )
        assert r_exp.status_code == 409, r_exp.text
        assert r_exp.json()["error"]["code"] == "HOLD_EXPIRED"

    # Verify expiration is persisted, exactly one hold_expired event, and next waiter promoted
    async with sessionmaker() as session:
        b_exp_row = (
            await session.execute(select(Booking).where(Booking.id == booking_exp.id))
        ).scalar_one()
        assert b_exp_row.status == "expired"
        assert b_exp_row.expires_at is None
        assert b_exp_row.version == 2

        e_exp_row = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_exp.id))
        ).scalar_one()
        assert e_exp_row.status == "expired"
        assert e_exp_row.version == 2

        exp_events = (
            (
                await session.execute(
                    select(Outbox).where(
                        Outbox.aggregate_id == booking_exp.id,
                        Outbox.event_type == "hold_expired",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(exp_events) == 1

        # Next waiter promoted!
        next_row = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_next.id))
        ).scalar_one()
        assert next_row.status == "offered"
        assert next_row.offered_booking_id is not None


@pytest.mark.asyncio
async def test_accept_vs_decline_race() -> None:
    """Concurrent accept and decline on an offered entry yields exactly one legal

    terminal outcome.
    """
    resource = await create_resource()
    user = await create_user()
    s, e = aligned_slot(days=6)
    now = datetime.now(timezone.utc)

    entry, booking = await create_offered_pair(
        resource=resource,
        user=user,
        starts_at=s,
        ends_at=e,
        expires_at=now + timedelta(minutes=15),
    )

    async with make_client() as client:
        t_accept = client.post(
            f"/api/v1/waitlist/{entry.id}/accept",
            json={},
            headers={**auth(user), "If-Match": '"1"'},
        )
        t_decline = client.delete(
            f"/api/v1/waitlist/{entry.id}",
            headers={**auth(user), "If-Match": '"1"'},
        )
        r_accept, r_decline = await asyncio.gather(t_accept, t_decline)

    statuses = {r_accept.status_code, r_decline.status_code}
    # One succeeds (200), the other sees modified version -> 412 VERSION_MISMATCH
    # or invalid state -> 409 INVALID_STATE
    assert 200 in statuses
    other_status = (statuses - {200}).pop() if len(statuses) > 1 else 200
    assert other_status in (409, 412)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stored_entry = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry.id))
        ).scalar_one()
        stored_booking = (
            await session.execute(select(Booking).where(Booking.id == booking.id))
        ).scalar_one()

        if r_accept.status_code == 200:
            assert stored_entry.status == "accepted"
            assert stored_booking.status == "confirmed"
        else:
            assert stored_entry.status == "cancelled"
            assert stored_booking.status == "cancelled"


@pytest.mark.asyncio
async def test_foreign_entry_returns_404() -> None:
    """A foreign entry id returns 404 for member and admin alike; E14/E15/E16 never

    expose another user's entry.
    """
    resource = await create_resource()
    owner = await create_user()
    other_member = await create_user()
    admin_user = await create_user(role="admin")
    s, e = aligned_slot(days=7)
    now = datetime.now(timezone.utc)

    entry, _ = await create_offered_pair(
        resource=resource,
        user=owner,
        starts_at=s,
        ends_at=e,
        expires_at=now + timedelta(minutes=15),
    )

    async with make_client() as client:
        # Member foreign decline -> 404
        r_mem_dec = await client.delete(
            f"/api/v1/waitlist/{entry.id}",
            headers={**auth(other_member), "If-Match": '"1"'},
        )
        assert r_mem_dec.status_code == 404
        assert r_mem_dec.json()["error"]["code"] == "NOT_FOUND"

        # Admin foreign decline -> 404 (own_waitlist policy applies equally to admins)
        r_adm_dec = await client.delete(
            f"/api/v1/waitlist/{entry.id}",
            headers={**auth(admin_user), "If-Match": '"1"'},
        )
        assert r_adm_dec.status_code == 404
        assert r_adm_dec.json()["error"]["code"] == "NOT_FOUND"

        # Member foreign accept -> 404
        r_mem_acc = await client.post(
            f"/api/v1/waitlist/{entry.id}/accept",
            json={},
            headers={**auth(other_member), "If-Match": '"1"'},
        )
        assert r_mem_acc.status_code == 404
        assert r_mem_acc.json()["error"]["code"] == "NOT_FOUND"

        # Admin foreign accept -> 404
        r_adm_acc = await client.post(
            f"/api/v1/waitlist/{entry.id}/accept",
            json={},
            headers={**auth(admin_user), "If-Match": '"1"'},
        )
        assert r_adm_acc.status_code == 404
        assert r_adm_acc.json()["error"]["code"] == "NOT_FOUND"

        # List routes: neither other_member nor admin see owner's entry
        r_mem_list = await client.get("/api/v1/waitlist", headers=auth(other_member))
        assert r_mem_list.status_code == 200
        assert r_mem_list.json()["total"] == 0

        r_adm_list = await client.get("/api/v1/waitlist", headers=auth(admin_user))
        assert r_adm_list.status_code == 200
        assert r_adm_list.json()["total"] == 0


@pytest.mark.asyncio
async def test_stale_version_and_precondition_headers() -> None:
    """Stale version returns 412; missing If-Match returns 428; malformed If-Match returns 422."""
    resource = await create_resource()
    user = await create_user()
    s, e = aligned_slot(days=8)
    now = datetime.now(timezone.utc)

    entry, _ = await create_offered_pair(
        resource=resource,
        user=user,
        starts_at=s,
        ends_at=e,
        expires_at=now + timedelta(minutes=15),
        entry_version=2,
    )

    async with make_client() as client:
        # Stale If-Match -> 412
        r_stale = await client.post(
            f"/api/v1/waitlist/{entry.id}/accept",
            json={},
            headers={**auth(user), "If-Match": '"1"'},
        )
        assert r_stale.status_code == 412
        assert r_stale.json()["error"]["code"] == "VERSION_MISMATCH"

        # Missing If-Match -> 428 PRECONDITION_REQUIRED
        r_missing = await client.post(
            f"/api/v1/waitlist/{entry.id}/accept",
            json={},
            headers=auth(user),
        )
        assert r_missing.status_code == 428
        assert r_missing.json()["error"]["code"] == "PRECONDITION_REQUIRED"

        # Malformed If-Match -> 422 VALIDATION_ERROR
        r_malformed = await client.post(
            f"/api/v1/waitlist/{entry.id}/accept",
            json={},
            headers={**auth(user), "If-Match": "not-quoted-or-weak"},
        )
        assert r_malformed.status_code == 422
        assert r_malformed.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_decline_offered_entry_cancels_and_promotes_next_waiter() -> None:
    """Declining an offered entry cancels both entry and booking, and promotes the next waiter."""
    resource = await create_resource()
    user_offered = await create_user()
    user_next = await create_user()
    s, e = aligned_slot(days=9)
    now = datetime.now(timezone.utc)

    entry_offered, booking_offered = await create_offered_pair(
        resource=resource,
        user=user_offered,
        starts_at=s,
        ends_at=e,
        expires_at=now + timedelta(minutes=15),
    )

    entry_next = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=user_next.id,
        resource_id=resource.id,
        time_range=Range(s, e, bounds="[)"),
        status="waiting",
        version=1,
        created_at=now + timedelta(seconds=1),
        updated_at=now + timedelta(seconds=1),
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(entry_next)

    async with make_client() as client:
        r_dec = await client.delete(
            f"/api/v1/waitlist/{entry_offered.id}",
            headers={**auth(user_offered), "If-Match": '"1"'},
        )
        assert r_dec.status_code == 200
        assert r_dec.json()["status"] == "cancelled"
        assert r_dec.json()["version"] == 2

    # Verify entry and booking cancelled, and next waiter promoted
    async with sessionmaker() as session:
        b_row = (
            await session.execute(select(Booking).where(Booking.id == booking_offered.id))
        ).scalar_one()
        assert b_row.status == "cancelled"
        assert b_row.version == 2
        assert b_row.expires_at is None

        next_row = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_next.id))
        ).scalar_one()
        assert next_row.status == "offered"
        assert next_row.offered_booking_id is not None

        # R6: Assert self-decline of an offered entry emits no booking_cancelled event
        cancelled_events = (
            (
                await session.execute(
                    select(Outbox).where(
                        Outbox.aggregate_id == booking_offered.id,
                        Outbox.event_type == "booking_cancelled",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(cancelled_events) == 0

        # The promoted waiter received their waitlist_offered event
        promoted_events = (
            (
                await session.execute(
                    select(Outbox).where(
                        Outbox.aggregate_id == next_row.offered_booking_id,
                        Outbox.event_type == "waitlist_offered",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(promoted_events) == 1


@pytest.mark.asyncio
async def test_withdraw_waiting_entry_changes_only_entry() -> None:
    """Withdrawing a waiting entry yields 200, version+1, status cancelled, no

    booking and no event.
    """
    resource = await create_resource()
    user = await create_user()
    s, e = aligned_slot(days=10)
    now = datetime.now(timezone.utc)

    entry = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=user.id,
        resource_id=resource.id,
        time_range=Range(s, e, bounds="[)"),
        status="waiting",
        version=1,
        created_at=now,
        updated_at=now,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(entry)

    async with sessionmaker() as session:
        events_before = (await session.execute(select(Outbox))).scalars().all()

    async with make_client() as client:
        r_dec = await client.delete(
            f"/api/v1/waitlist/{entry.id}",
            headers={**auth(user), "If-Match": '"1"'},
        )
        assert r_dec.status_code == 200
        assert r_dec.json()["status"] == "cancelled"
        assert r_dec.json()["version"] == 2

    async with sessionmaker() as session:
        row = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry.id))
        ).scalar_one()
        assert row.status == "cancelled"
        assert row.version == 2
        assert row.offered_booking_id is None

        # Verify no booking created
        bookings = (
            (await session.execute(select(Booking).where(Booking.resource_id == resource.id)))
            .scalars()
            .all()
        )
        assert len(bookings) == 0

        # Verify no new outbox events created
        events_after = (await session.execute(select(Outbox))).scalars().all()
        assert len(events_after) == len(events_before)


@pytest.mark.asyncio
async def test_cancelling_confirmed_booking_does_not_rewrite_accepted_entry() -> None:
    """Cancelling a confirmed booking through E12 does not rewrite an accepted waitlist

    entry (R1).
    """
    resource = await create_resource()
    user = await create_user()
    s, e = aligned_slot(days=11)
    now = datetime.now(timezone.utc)

    # 1. User is offered and accepts the booking
    entry, booking = await create_offered_pair(
        resource=resource,
        user=user,
        starts_at=s,
        ends_at=e,
        expires_at=now + timedelta(minutes=15),
    )

    async with make_client() as client:
        r_accept = await client.post(
            f"/api/v1/waitlist/{entry.id}/accept",
            json={},
            headers={**auth(user), "If-Match": '"1"'},
        )
        assert r_accept.status_code == 200
        assert r_accept.json()["status"] == "confirmed"
        assert r_accept.json()["version"] == 2

        # 2. Cancel the now-confirmed reservation via E12
        r_cancel = await client.post(
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": "cancelling confirmed reservation"},
            headers={**auth(user), "If-Match": '"2"'},
        )
        assert r_cancel.status_code == 200
        assert r_cancel.json()["status"] == "cancelled"
        assert r_cancel.json()["version"] == 3

    # 3. Assert the waitlist entry remains 'accepted' with its version unchanged at 2
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stored_entry = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry.id))
        ).scalar_one()
        assert stored_entry.status == "accepted"
        assert stored_entry.version == 2
        assert stored_entry.offered_booking_id == booking.id

        # Assert no other waitlist entries were modified or created
        all_entries = (
            (
                await session.execute(
                    select(WaitlistEntry).where(WaitlistEntry.resource_id == resource.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(all_entries) == 1


@pytest.mark.asyncio
async def test_accept_offer_with_distinct_entry_and_booking_versions() -> None:
    """Spec 11.6 v1.1: Accept with distinct entry_version=3 and booking_version=1 (R9)."""
    resource = await create_resource()
    user = await create_user()
    s, e = aligned_slot(days=12)
    now = datetime.now(timezone.utc)

    # Offered pair with entry_version=3, booking_version=1
    entry, booking = await create_offered_pair(
        resource=resource,
        user=user,
        starts_at=s,
        ends_at=e,
        expires_at=now + timedelta(minutes=15),
        entry_version=3,
        booking_version=1,
    )

    async with make_client() as client:
        # If-Match: "1" pins stale entry version -> returns 412 VERSION_MISMATCH
        r_stale = await client.post(
            f"/api/v1/waitlist/{entry.id}/accept",
            json={},
            headers={**auth(user), "If-Match": '"1"'},
        )
        assert r_stale.status_code == 412
        assert r_stale.json()["error"]["code"] == "VERSION_MISMATCH"

        # If-Match: "3" pins current entry version -> succeeds 200
        r_accept = await client.post(
            f"/api/v1/waitlist/{entry.id}/accept",
            json={},
            headers={**auth(user), "If-Match": '"3"'},
        )
        assert r_accept.status_code == 200, r_accept.text
        data = r_accept.json()
        assert data["id"] == str(booking.id)
        assert data["status"] == "confirmed"
        # Booking version was 1, increments to 2
        assert data["version"] == 2
        assert data["expires_at"] is None
        assert r_accept.headers.get("etag") == '"2"'

    # Verify database state and outbox event
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        b_row = (
            await session.execute(select(Booking).where(Booking.id == booking.id))
        ).scalar_one()
        assert b_row.status == "confirmed"
        assert b_row.version == 2
        assert b_row.expires_at is None

        e_row = (
            await session.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry.id))
        ).scalar_one()
        assert e_row.status == "accepted"
        # Entry version was 3, increments to 4
        assert e_row.version == 4

        events = (
            (
                await session.execute(
                    select(Outbox).where(
                        Outbox.aggregate_id == booking.id,
                        Outbox.event_type == "booking_confirmed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].aggregate_version == 2
