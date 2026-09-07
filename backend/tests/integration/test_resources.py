import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range

from alembic import command

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.auth.models import User
from app.auth.passwords import hash_password
from app.bookings.models import Booking
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app
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


async def create_booking(
    *,
    resource_id: uuid.UUID,
    user_id: uuid.UUID | None,
    created_by: uuid.UUID,
    kind: str = "reservation",
    t_start: datetime,
    t_end: datetime,
    status: str = "confirmed",
    expires_at: datetime | None = None,
) -> Booking:
    booking = Booking(
        id=uuid.uuid4(),
        resource_id=resource_id,
        user_id=user_id,
        created_by=created_by,
        kind=kind,
        time_range=Range(t_start, t_end, bounds="[)"),
        status=status,
        expires_at=expires_at,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(booking)
    return booking


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


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://localhost:5173",
    )


@pytest.mark.asyncio
async def test_e06_pagination_and_ordering() -> None:
    """E06 lists active resources ordered by name asc, id asc, excluding archived."""
    user = await create_user(role="member", enabled=True)
    token = make_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    prefix = f"E06_{uuid.uuid4().hex[:6]}"
    # Seed 30 active resources with distinctive prefix
    for i in range(30):
        await create_resource(name=f"{prefix}_active_{i:02d}", active=True)
    # Seed 5 archived resources with same prefix
    for i in range(5):
        await create_resource(name=f"{prefix}_archived_{i:02d}", active=False)

    async with make_client() as client:
        # Default pagination: limit=25, offset=0
        r_page1 = await client.get("/api/v1/resources", headers=headers)
        assert r_page1.status_code == 200
        p1 = r_page1.json()
        assert p1["limit"] == 25
        assert p1["offset"] == 0
        assert p1["total"] >= 30

        # Filter items matching our test prefix
        # Fetch all pages matching prefix
        all_items: list[dict] = []
        offset = 0
        limit = 20
        while True:
            resp = await client.get(
                f"/api/v1/resources?limit={limit}&offset={offset}", headers=headers
            )
            assert resp.status_code == 200
            data = resp.json()
            items = data["items"]
            if not items:
                break
            for item in items:
                if item["name"].startswith(prefix):
                    all_items.append(item)
            offset += limit
            if offset >= data["total"]:
                break

        # Assert exactly 30 items found, all active=True, no archived
        assert len(all_items) == 30
        assert all(it["active"] is True for it in all_items)
        assert not any("archived" in it["name"] for it in all_items)

        # Assert sorted by name ascending
        names = [it["name"] for it in all_items]
        assert names == sorted(names)

        # Verify validation error on out-of-range pagination bounds
        bad_limit_0 = await client.get("/api/v1/resources?limit=0", headers=headers)
        assert bad_limit_0.status_code == 422
        assert bad_limit_0.json()["error"]["code"] == "VALIDATION_ERROR"

        bad_limit_101 = await client.get("/api/v1/resources?limit=101", headers=headers)
        assert bad_limit_101.status_code == 422
        assert bad_limit_101.json()["error"]["code"] == "VALIDATION_ERROR"

        bad_offset_neg = await client.get("/api/v1/resources?offset=-1", headers=headers)
        assert bad_offset_neg.status_code == 422
        assert bad_offset_neg.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_e07_get_resource_and_etag() -> None:
    """E07 returns resource with ETag header; archived and missing produce identical 404."""
    user = await create_user(role="member", enabled=True)
    token = make_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    res_active = await create_resource(
        name=f"Active_{uuid.uuid4().hex[:6]}", active=True, version=3
    )
    res_archived = await create_resource(name=f"Archived_{uuid.uuid4().hex[:6]}", active=False)
    unknown_id = uuid.uuid4()

    async with make_client() as client:
        # Active resource -> 200 with ETag: "<version>"
        r_act = await client.get(f"/api/v1/resources/{res_active.id}", headers=headers)
        assert r_act.status_code == 200
        assert r_act.headers.get("etag") == '"3"'
        body = r_act.json()
        assert body["id"] == str(res_active.id)
        assert body["name"] == res_active.name
        assert body["active"] is True
        assert body["version"] == 3

        # Archived resource -> 404 NOT_FOUND
        r_arch = await client.get(f"/api/v1/resources/{res_archived.id}", headers=headers)
        assert r_arch.status_code == 404
        assert r_arch.json()["error"]["code"] == "NOT_FOUND"

        # Unknown UUID -> 404 NOT_FOUND
        r_unk = await client.get(f"/api/v1/resources/{unknown_id}", headers=headers)
        assert r_unk.status_code == 404
        assert r_unk.json()["error"]["code"] == "NOT_FOUND"

        # Byte-identical 404 responses ensure archived resource existence is not disclosed
        assert r_arch.content == r_unk.content


@pytest.mark.asyncio
async def test_e08_adjacency() -> None:
    """Adjacent half-open intervals [) do not occupy; single-slot overlap does."""
    user = await create_user(role="member", enabled=True)
    token = make_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    r = await create_resource(active=True)

    # Query window: [10:00, 14:00) UTC
    w_start = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    w_end = datetime(2026, 7, 1, 14, 0, 0, tzinfo=timezone.utc)

    # Booking 1: ends exactly at window start [08:00, 10:00) -> NOT occupied
    await create_booking(
        resource_id=r.id,
        user_id=user.id,
        created_by=user.id,
        t_start=datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc),
        t_end=w_start,
        status="confirmed",
    )

    # Booking 2: starts exactly at window end [14:00, 16:00) -> NOT occupied
    await create_booking(
        resource_id=r.id,
        user_id=user.id,
        created_by=user.id,
        t_start=w_end,
        t_end=datetime(2026, 7, 1, 16, 0, 0, tzinfo=timezone.utc),
        status="confirmed",
    )

    # Booking 3: overlaps by single 30-minute slot [11:00, 11:30) -> OCCUPIED
    await create_booking(
        resource_id=r.id,
        user_id=user.id,
        created_by=user.id,
        t_start=datetime(2026, 7, 1, 11, 0, 0, tzinfo=timezone.utc),
        t_end=datetime(2026, 7, 1, 11, 30, 0, tzinfo=timezone.utc),
        status="confirmed",
    )

    async with make_client() as client:
        params = {
            "starts_at": "2026-07-01T10:00:00Z",
            "ends_at": "2026-07-01T14:00:00Z",
        }
        res = await client.get(
            f"/api/v1/resources/{r.id}/availability", params=params, headers=headers
        )
        assert res.status_code == 200
        data = res.json()
        assert data["resource_id"] == str(r.id)
        assert data["timezone"] == "America/New_York"
        assert len(data["occupied"]) == 1
        assert data["occupied"][0]["starts_at"] == "2026-07-01T11:00:00.000000Z"
        assert data["occupied"][0]["ends_at"] == "2026-07-01T11:30:00.000000Z"

    # DATABASE INVARIANT ASSERTION: independently query bookings table
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        query_range = func.tstzrange(w_start, w_end, "[)")
        db_stmt = (
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.resource_id == r.id,
                Booking.status.in_(["confirmed", "offered"]),
                Booking.time_range.op("&&")(query_range),
            )
        )
        db_count = (await session.execute(db_stmt)).scalar_one()
        assert len(data["occupied"]) == db_count == 1


@pytest.mark.asyncio
async def test_e08_status_filtering() -> None:
    """Only 'confirmed' and 'offered' occupy; 'pending', 'cancelled', 'expired' do not."""
    user = await create_user(role="member", enabled=True)
    token = make_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    r = await create_resource(active=True)

    w_start = datetime(2026, 7, 2, 10, 0, 0, tzinfo=timezone.utc)
    w_end = datetime(2026, 7, 2, 16, 0, 0, tzinfo=timezone.utc)

    # 1. confirmed -> occupies
    await create_booking(
        resource_id=r.id,
        user_id=user.id,
        created_by=user.id,
        t_start=datetime(2026, 7, 2, 10, 0, 0, tzinfo=timezone.utc),
        t_end=datetime(2026, 7, 2, 11, 0, 0, tzinfo=timezone.utc),
        status="confirmed",
    )

    # 2. offered (requires expires_at <= lower(time_range)) -> occupies
    t_offered_start = datetime(2026, 7, 2, 11, 0, 0, tzinfo=timezone.utc)
    await create_booking(
        resource_id=r.id,
        user_id=user.id,
        created_by=user.id,
        t_start=t_offered_start,
        t_end=datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc),
        status="offered",
        expires_at=t_offered_start,
    )

    # 3. pending -> does NOT occupy
    await create_booking(
        resource_id=r.id,
        user_id=user.id,
        created_by=user.id,
        t_start=datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc),
        t_end=datetime(2026, 7, 2, 13, 0, 0, tzinfo=timezone.utc),
        status="pending",
    )

    # 4. cancelled -> does NOT occupy
    await create_booking(
        resource_id=r.id,
        user_id=user.id,
        created_by=user.id,
        t_start=datetime(2026, 7, 2, 13, 0, 0, tzinfo=timezone.utc),
        t_end=datetime(2026, 7, 2, 14, 0, 0, tzinfo=timezone.utc),
        status="cancelled",
    )

    # 5. expired -> does NOT occupy
    await create_booking(
        resource_id=r.id,
        user_id=user.id,
        created_by=user.id,
        t_start=datetime(2026, 7, 2, 14, 0, 0, tzinfo=timezone.utc),
        t_end=datetime(2026, 7, 2, 15, 0, 0, tzinfo=timezone.utc),
        status="expired",
    )

    async with make_client() as client:
        params = {
            "starts_at": "2026-07-02T10:00:00Z",
            "ends_at": "2026-07-02T16:00:00Z",
        }
        res = await client.get(
            f"/api/v1/resources/{r.id}/availability", params=params, headers=headers
        )
        assert res.status_code == 200
        data = res.json()
        occupied = data["occupied"]
        assert len(occupied) == 2
        statuses = [item["status"] for item in occupied]
        assert statuses == ["confirmed", "offered"]

    # DATABASE INVARIANT ASSERTION: independently query bookings table
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        query_range = func.tstzrange(w_start, w_end, "[)")
        db_stmt = (
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.resource_id == r.id,
                Booking.status.in_(["confirmed", "offered"]),
                Booking.time_range.op("&&")(query_range),
            )
        )
        db_count = (await session.execute(db_stmt)).scalar_one()
        assert len(occupied) == db_count == 2


@pytest.mark.asyncio
async def test_e08_kinds() -> None:
    """Both reservation and blackout kinds appear with exact kind value."""
    user = await create_user(role="member", enabled=True)
    token = make_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    r = await create_resource(active=True)

    # Reservation kind
    await create_booking(
        resource_id=r.id,
        user_id=user.id,
        created_by=user.id,
        kind="reservation",
        t_start=datetime(2026, 7, 3, 10, 0, 0, tzinfo=timezone.utc),
        t_end=datetime(2026, 7, 3, 11, 0, 0, tzinfo=timezone.utc),
        status="confirmed",
    )

    # Blackout kind
    await create_booking(
        resource_id=r.id,
        user_id=None,
        created_by=user.id,
        kind="blackout",
        t_start=datetime(2026, 7, 3, 11, 0, 0, tzinfo=timezone.utc),
        t_end=datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc),
        status="confirmed",
    )

    async with make_client() as client:
        params = {
            "starts_at": "2026-07-03T10:00:00Z",
            "ends_at": "2026-07-03T12:00:00Z",
        }
        res = await client.get(
            f"/api/v1/resources/{r.id}/availability", params=params, headers=headers
        )
        assert res.status_code == 200
        data = res.json()
        occupied = data["occupied"]
        assert len(occupied) == 2
        kinds = [it["kind"] for it in occupied]
        assert kinds == ["reservation", "blackout"]


@pytest.mark.asyncio
async def test_e08_personal_field_absence() -> None:
    """Availability response contains NO owner or identifier fields, exactly 4 keys per interval."""
    secret_email = "very_distinctive_owner_98765@example.com"
    secret_display_name = "AgentSecretDisplayNameXYZ"
    owner = await create_user(
        email=secret_email,
        display_name=secret_display_name,
        role="member",
        enabled=True,
    )

    caller = await create_user(role="member", enabled=True)
    token = make_token(caller)
    headers = {"Authorization": f"Bearer {token}"}

    r = await create_resource(active=True)

    b = await create_booking(
        resource_id=r.id,
        user_id=owner.id,
        created_by=owner.id,
        t_start=datetime(2026, 7, 4, 10, 0, 0, tzinfo=timezone.utc),
        t_end=datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc),
        status="confirmed",
    )

    async with make_client() as client:
        params = {
            "starts_at": "2026-07-04T10:00:00Z",
            "ends_at": "2026-07-04T12:00:00Z",
        }
        res = await client.get(
            f"/api/v1/resources/{r.id}/availability", params=params, headers=headers
        )
        assert res.status_code == 200

        # Full raw response text checks: privacy control assertion
        raw_text = res.text
        assert secret_email not in raw_text
        assert secret_display_name not in raw_text
        assert str(owner.id) not in raw_text
        assert str(b.id) not in raw_text

        data = res.json()
        assert len(data["occupied"]) == 1
        interval = data["occupied"][0]
        # Assert each occupied entry has EXACTLY the 4 required keys
        assert set(interval.keys()) == {"starts_at", "ends_at", "kind", "status"}


@pytest.mark.asyncio
async def test_e08_unclipped_range_and_ordering() -> None:
    """Actual booking range is returned unclipped, ordered by lower(time_range) asc, then id asc."""
    user = await create_user(role="member", enabled=True)
    token = make_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    r = await create_resource(active=True)

    # Booking spans [09:00, 13:00)
    await create_booking(
        resource_id=r.id,
        user_id=user.id,
        created_by=user.id,
        t_start=datetime(2026, 7, 5, 9, 0, 0, tzinfo=timezone.utc),
        t_end=datetime(2026, 7, 5, 13, 0, 0, tzinfo=timezone.utc),
        status="confirmed",
    )

    # Query window is narrower: [10:00, 12:00)
    async with make_client() as client:
        params = {
            "starts_at": "2026-07-05T10:00:00Z",
            "ends_at": "2026-07-05T12:00:00Z",
        }
        res = await client.get(
            f"/api/v1/resources/{r.id}/availability", params=params, headers=headers
        )
        assert res.status_code == 200
        data = res.json()
        assert len(data["occupied"]) == 1
        occ = data["occupied"][0]
        # Must return actual booking bounds, NOT clipped by query window
        assert occ["starts_at"] == "2026-07-05T09:00:00.000000Z"
        assert occ["ends_at"] == "2026-07-05T13:00:00.000000Z"


@pytest.mark.asyncio
async def test_e08_invalid_windows() -> None:
    """Invalid availability windows return 422 INVALID_WINDOW."""
    user = await create_user(role="member", enabled=True)
    token = make_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    r = await create_resource(active=True)

    invalid_cases = [
        # Unaligned minute
        {"starts_at": "2026-07-06T10:15:00Z", "ends_at": "2026-07-06T11:00:00Z"},
        # Nonzero second
        {"starts_at": "2026-07-06T10:00:01Z", "ends_at": "2026-07-06T11:00:00Z"},
        # Nonzero microsecond
        {"starts_at": "2026-07-06T10:00:00.000001Z", "ends_at": "2026-07-06T11:00:00Z"},
        # Naive timestamp
        {"starts_at": "2026-07-06T10:00:00", "ends_at": "2026-07-06T11:00:00Z"},
        # ends_at == starts_at
        {"starts_at": "2026-07-06T10:00:00Z", "ends_at": "2026-07-06T10:00:00Z"},
        # ends_at < starts_at
        {"starts_at": "2026-07-06T12:00:00Z", "ends_at": "2026-07-06T10:00:00Z"},
        # Span of 7 days + 30 minutes
        {"starts_at": "2026-07-06T10:00:00Z", "ends_at": "2026-07-13T10:30:00Z"},
    ]

    async with make_client() as client:
        for params in invalid_cases:
            res = await client.get(
                f"/api/v1/resources/{r.id}/availability", params=params, headers=headers
            )
            assert res.status_code == 422, (
                f"Expected 422 for params {params}, got {res.status_code}"
            )
            assert res.json()["error"]["code"] == "INVALID_WINDOW"


@pytest.mark.asyncio
async def test_e08_archived_resource_returns_404() -> None:
    """Archived resource availability returns 404 NOT_FOUND identical to missing UUID."""
    user = await create_user(role="member", enabled=True)
    token = make_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    r_archived = await create_resource(active=False)
    missing_id = uuid.uuid4()

    params = {
        "starts_at": "2026-07-07T10:00:00Z",
        "ends_at": "2026-07-07T12:00:00Z",
    }

    async with make_client() as client:
        res_arch = await client.get(
            f"/api/v1/resources/{r_archived.id}/availability", params=params, headers=headers
        )
        assert res_arch.status_code == 404
        assert res_arch.json()["error"]["code"] == "NOT_FOUND"

        res_missing = await client.get(
            f"/api/v1/resources/{missing_id}/availability", params=params, headers=headers
        )
        assert res_missing.status_code == 404
        assert res_missing.json()["error"]["code"] == "NOT_FOUND"

        assert res_arch.content == res_missing.content


@pytest.mark.asyncio
async def test_e08_cross_resource_isolation() -> None:
    """Availability queries on a resource exclude bookings belonging to other resources."""
    user = await create_user(role="member", enabled=True)
    token = make_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    resource_a = await create_resource(active=True)
    resource_b = await create_resource(active=True)

    w_start = datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)
    w_end = datetime(2026, 7, 10, 14, 0, 0, tzinfo=timezone.utc)

    # Confirmed booking on resource B only
    await create_booking(
        resource_id=resource_b.id,
        user_id=user.id,
        created_by=user.id,
        t_start=datetime(2026, 7, 10, 11, 0, 0, tzinfo=timezone.utc),
        t_end=datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc),
        status="confirmed",
    )

    params = {
        "starts_at": "2026-07-10T10:00:00Z",
        "ends_at": "2026-07-10T14:00:00Z",
    }

    async with make_client() as client:
        # Resource A should have no occupied intervals
        res_a = await client.get(
            f"/api/v1/resources/{resource_a.id}/availability", params=params, headers=headers
        )
        assert res_a.status_code == 200
        data_a = res_a.json()
        assert data_a["resource_id"] == str(resource_a.id)
        assert data_a["occupied"] == []

        # Resource B should have exactly 1 occupied interval
        res_b = await client.get(
            f"/api/v1/resources/{resource_b.id}/availability", params=params, headers=headers
        )
        assert res_b.status_code == 200
        data_b = res_b.json()
        assert data_b["resource_id"] == str(resource_b.id)
        assert len(data_b["occupied"]) == 1
        assert data_b["occupied"][0]["starts_at"] == "2026-07-10T11:00:00.000000Z"
        assert data_b["occupied"][0]["ends_at"] == "2026-07-10T12:00:00.000000Z"

    # Database query asserting qualifying row counts per resource
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        query_range = func.tstzrange(w_start, w_end, "[)")

        db_stmt_a = (
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.resource_id == resource_a.id,
                Booking.status.in_(["confirmed", "offered"]),
                Booking.time_range.op("&&")(query_range),
            )
        )
        db_count_a = (await session.execute(db_stmt_a)).scalar_one()
        assert len(data_a["occupied"]) == db_count_a == 0

        db_stmt_b = (
            select(func.count())
            .select_from(Booking)
            .where(
                Booking.resource_id == resource_b.id,
                Booking.status.in_(["confirmed", "offered"]),
                Booking.time_range.op("&&")(query_range),
            )
        )
        db_count_b = (await session.execute(db_stmt_b)).scalar_one()
        assert len(data_b["occupied"]) == db_count_b == 1
