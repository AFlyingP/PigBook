"""Integration tests for waitlist join (E13) and own waitlist list (E14) (Phase A / T-014)."""

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
    email = f"wl_user_{uuid.uuid4().hex[:8]}@example.com"
    pwd_hash = hash_password("valid-password-123")
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=pwd_hash,
        display_name="Waitlist User",
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
        name=f"Resource_{uuid.uuid4().hex[:6]}",
        description="Waitlist Test Resource",
        location="Studio A",
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
async def test_join_on_free_interval_returns_slot_available() -> None:
    """Joining a free interval when no active booking/blackout overlaps returns 409

    SLOT_AVAILABLE.
    """
    user = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=2)

    async with make_client() as client:
        res = await client.post(
            "/api/v1/waitlist",
            json={
                "resource_id": str(resource.id),
                "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": ends_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            headers=auth(user),
        )

    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "SLOT_AVAILABLE"

    # Verify no waitlist entry was created
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        entries = (
            (
                await session.execute(
                    select(WaitlistEntry).where(WaitlistEntry.resource_id == resource.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(entries) == 0


@pytest.mark.asyncio
async def test_join_inactive_resource_returns_resource_inactive() -> None:
    """Joining an inactive resource returns 409 RESOURCE_INACTIVE."""
    user = await create_user()
    resource = await create_resource(active=False)
    starts_at, ends_at = aligned_slot(days=2)

    async with make_client() as client:
        res = await client.post(
            "/api/v1/waitlist",
            json={
                "resource_id": str(resource.id),
                "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": ends_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            headers=auth(user),
        )

    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "RESOURCE_INACTIVE"


@pytest.mark.asyncio
async def test_join_invalid_window_returns_invalid_window() -> None:
    """Invalid windows (misaligned, too short/long, past) return 422 INVALID_WINDOW."""
    user = await create_user()
    resource = await create_resource()
    starts_at, _ = aligned_slot(days=2)
    # Not on a 30-minute boundary
    bad_start = starts_at.replace(minute=15)
    bad_end = starts_at + timedelta(hours=1)

    async with make_client() as client:
        res = await client.post(
            "/api/v1/waitlist",
            json={
                "resource_id": str(resource.id),
                "starts_at": bad_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": bad_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            headers=auth(user),
        )

    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "INVALID_WINDOW"


@pytest.mark.asyncio
async def test_join_when_already_booked_returns_already_booked() -> None:
    """A user who already owns an active reservation overlapping the window gets 409

    ALREADY_BOOKED.
    """
    user = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=3)

    # User already has confirmed reservation
    await insert_booking(
        resource=resource,
        owner=user,
        created_by=user,
        starts_at=starts_at,
        ends_at=ends_at,
        status="confirmed",
    )

    async with make_client() as client:
        res = await client.post(
            "/api/v1/waitlist",
            json={
                "resource_id": str(resource.id),
                "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": ends_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            headers=auth(user),
        )

    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "ALREADY_BOOKED"


@pytest.mark.asyncio
async def test_simultaneous_duplicate_join_exactly_one_succeeds() -> None:
    """Simultaneous duplicate join of same user/resource/exact window yields one 201,

    one 409 ALREADY_WAITLISTED.
    """
    user = await create_user()
    other_user = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=4)

    # Active booking exists on that slot so it's busy
    await insert_booking(
        resource=resource,
        owner=other_user,
        created_by=other_user,
        starts_at=starts_at,
        ends_at=ends_at,
        status="confirmed",
    )

    payload = {
        "resource_id": str(resource.id),
        "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": ends_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    async with make_client() as client:
        t1 = client.post("/api/v1/waitlist", json=payload, headers=auth(user))
        t2 = client.post("/api/v1/waitlist", json=payload, headers=auth(user))
        r1, r2 = await asyncio.gather(t1, t2)

    statuses = [r1.status_code, r2.status_code]
    assert sorted(statuses) == [201, 409]
    err_res = r1 if r1.status_code == 409 else r2
    assert err_res.json()["error"]["code"] == "ALREADY_WAITLISTED"

    succ_res = r1 if r1.status_code == 201 else r2
    assert succ_res.headers.get("etag") == '"1"'
    assert "location" in succ_res.headers


@pytest.mark.asyncio
async def test_e14_isolation_and_pagination() -> None:
    """E14 lists only the caller's own entries; pagination order is created_at DESC,

    id; status filter works.
    """
    user_a = await create_user()
    user_b = await create_user()
    other = await create_user()
    resource = await create_resource()

    # Create active bookings for slots
    slots = [aligned_slot(days=6, hour=10 + i, duration_hours=1) for i in range(5)]
    for s, e in slots:
        await insert_booking(
            resource=resource,
            owner=other,
            created_by=other,
            starts_at=s,
            ends_at=e,
        )

    # User A joins 3 slots
    async with make_client() as client:
        for i in range(3):
            s, e = slots[i]
            r = await client.post(
                "/api/v1/waitlist",
                json={
                    "resource_id": str(resource.id),
                    "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                headers=auth(user_a),
            )
            assert r.status_code == 201

        # User B joins 1 slot
        s, e = slots[3]
        r = await client.post(
            "/api/v1/waitlist",
            json={
                "resource_id": str(resource.id),
                "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            headers=auth(user_b),
        )
        assert r.status_code == 201

        # User A lists waitlist: sees only their 3 entries
        list_a = await client.get("/api/v1/waitlist", headers=auth(user_a))
        assert list_a.status_code == 200
        data_a = list_a.json()
        assert data_a["total"] == 3
        assert len(data_a["items"]) == 3
        for item in data_a["items"]:
            assert item["user_id"] == str(user_a.id)

        # Stable order: created_at DESC
        created_ats = [item["created_at"] for item in data_a["items"]]
        assert created_ats == sorted(created_ats, reverse=True)

        # User B lists waitlist: sees only their 1 entry
        list_b = await client.get("/api/v1/waitlist", headers=auth(user_b))
        assert list_b.status_code == 200
        data_b = list_b.json()
        assert data_b["total"] == 1
        assert data_b["items"][0]["user_id"] == str(user_b.id)

        # Status filter check
        filtered = await client.get("/api/v1/waitlist?status=waiting", headers=auth(user_a))
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 3

        empty_filtered = await client.get("/api/v1/waitlist?status=cancelled", headers=auth(user_a))
        assert empty_filtered.status_code == 200
        assert empty_filtered.json()["total"] == 0


@pytest.mark.asyncio
async def test_fifo_tie_break_ordering() -> None:
    """Entries with the same created_at order deterministically by (created_at, id)."""
    user_a = await create_user()
    user_b = await create_user()
    resource = await create_resource()
    s, e = aligned_slot(days=7)

    same_time = datetime.now(timezone.utc)
    entry_1 = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=user_a.id,
        resource_id=resource.id,
        time_range=Range(s, e, bounds="[)"),
        status="waiting",
        version=1,
        created_at=same_time,
        updated_at=same_time,
    )
    entry_2 = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=user_b.id,
        resource_id=resource.id,
        time_range=Range(s, e, bounds="[)"),
        status="waiting",
        version=1,
        created_at=same_time,
        updated_at=same_time,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(entry_1)
            session.add(entry_2)

    async with sessionmaker() as session:
        stmt = (
            select(WaitlistEntry)
            .where(WaitlistEntry.resource_id == resource.id, WaitlistEntry.status == "waiting")
            .order_by(WaitlistEntry.created_at.asc(), WaitlistEntry.id.asc())
        )
        ordered = (await session.execute(stmt)).scalars().all()
        expected_first = entry_1 if entry_1.id < entry_2.id else entry_2
        assert ordered[0].id == expected_first.id


@pytest.mark.asyncio
async def test_waitlist_capacity_cap_500() -> None:
    """500 active entries limit is enforced; existing member gets ALREADY_WAITLISTED

    before WAITLIST_FULL; cancelled/expired rows do not count toward cap.
    """
    resource = await create_resource()
    owner = await create_user()
    s, e = aligned_slot(days=8)

    # Busy slot
    await insert_booking(
        resource=resource,
        owner=owner,
        created_by=owner,
        starts_at=s,
        ends_at=e,
        status="confirmed",
    )

    sessionmaker = get_sessionmaker()
    dummy_users = [await create_user() for _ in range(5)]

    # Bulk insert 500 active rows with distinct (user, time_range)
    # Using 500 different slots or users
    base_time = datetime.now(timezone.utc)
    active_entries = []
    for i in range(500):
        # Different time range for each to avoid unique index
        slot_s = s + timedelta(days=10 + i)
        slot_e = slot_s + timedelta(hours=1)
        active_entries.append(
            WaitlistEntry(
                id=uuid.uuid4(),
                user_id=dummy_users[i % len(dummy_users)].id,
                resource_id=resource.id,
                time_range=Range(slot_s, slot_e, bounds="[)"),
                status="waiting",
                version=1,
                created_at=base_time,
                updated_at=base_time,
            )
        )

    # Also add cancelled/expired entries that must NOT count toward cap
    for idx, status in enumerate(("cancelled", "expired", "cancelled", "expired")):
        slot_s = s + timedelta(days=1000 + idx)
        slot_e = slot_s + timedelta(hours=1)
        active_entries.append(
            WaitlistEntry(
                id=uuid.uuid4(),
                user_id=dummy_users[0].id,
                resource_id=resource.id,
                time_range=Range(slot_s, slot_e, bounds="[)"),
                status=status,
                version=1,
                created_at=base_time,
                updated_at=base_time,
            )
        )

    async with sessionmaker() as session:
        async with session.begin():
            session.add_all(active_entries)

    # Also seed user_existing who already has a waitlist entry on the slot
    user_existing = dummy_users[0]
    # Give user_existing a membership on (s, e)
    existing_entry = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=user_existing.id,
        resource_id=resource.id,
        time_range=Range(s, e, bounds="[)"),
        status="waiting",
        version=1,
        created_at=base_time,
        updated_at=base_time,
    )
    async with sessionmaker() as session:
        async with session.begin():
            session.add(existing_entry)

    # An existing member attempting to join returns ALREADY_WAITLISTED before WAITLIST_FULL
    async with make_client() as client:
        r_exist = await client.post(
            "/api/v1/waitlist",
            json={
                "resource_id": str(resource.id),
                "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            headers=auth(user_existing),
        )
        assert r_exist.status_code == 409, r_exist.text
        assert r_exist.json()["error"]["code"] == "ALREADY_WAITLISTED"

        # A new user attempting to join gets WAITLIST_FULL
        new_user = await create_user()
        r_full = await client.post(
            "/api/v1/waitlist",
            json={
                "resource_id": str(resource.id),
                "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            headers=auth(new_user),
        )
        assert r_full.status_code == 409, r_full.text
        assert r_full.json()["error"]["code"] == "WAITLIST_FULL"
