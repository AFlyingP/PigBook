import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy import func, select, text

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.auth.models import User
from app.auth.passwords import hash_password
from app.bookings.idempotency import _compute_request_hash
from app.bookings.models import Booking, IdempotencyKey
from app.bookings.schemas import BookingCreate
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app
from app.notifications.models import Outbox
from app.resources.models import Resource

get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _cleanup_engine() -> None:
    yield
    import app.db.session

    if app.db.session._engine is not None:
        await app.db.session._engine.dispose()
        app.db.session._engine = None
        app.db.session._sessionmaker = None
        app.db.session._engine_url = None


def make_client(ip: str) -> httpx.AsyncClient:
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


async def create_user(
    *,
    email: str | None = None,
    password: str = "valid-password-123",
    role: str = "member",
    enabled: bool = True,
) -> User:
    if email is None:
        email = f"idemp_{uuid.uuid4().hex[:8]}@example.com"
    pwd_hash = hash_password(password)
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=pwd_hash,
        display_name="Idempotency User",
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
    active: bool = True,
) -> Resource:
    if name is None:
        name = f"Resource_{uuid.uuid4().hex[:8]}"
    r = Resource(
        id=uuid.uuid4(),
        name=name,
        description="Idempotency test resource",
        location="Room 201",
        active=active,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(r)
    return r


def get_aligned_window(
    days_ahead: int = 2,
    hour: int = 10,
    minute: int = 0,
    duration_hours: int = 1,
) -> tuple[datetime, datetime]:
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    s = base + timedelta(days=days_ahead, hours=hour, minutes=minute)
    e = s + timedelta(hours=duration_hours)
    return s, e


@pytest.mark.asyncio
async def test_same_key_same_payload_replays() -> None:
    user = await create_user()
    token = make_token(user)
    resource = await create_resource()
    s, e = get_aligned_window(days_ahead=2, hour=10)

    key = str(uuid.uuid4())
    client_ip = f"10.1.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    payload = {
        "resource_id": str(resource.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }

    async with make_client(ip=client_ip) as client:
        # Initial execution
        r1 = await client.post("/api/v1/bookings", json=payload, headers=headers)
        assert r1.status_code == 201
        assert r1.headers.get("idempotency-replayed") == "false"
        assert "location" in r1.headers
        assert r1.headers.get("etag") == '"1"'
        req_id_1 = r1.headers.get("x-request-id")
        assert req_id_1 is not None

        data1 = r1.json()
        booking_id = data1["id"]
        assert data1["resource_id"] == str(resource.id)
        assert data1["user_id"] == str(user.id)
        assert data1["status"] == "confirmed"

        # Replay with same key and same payload
        r2 = await client.post("/api/v1/bookings", json=payload, headers=headers)
        assert r2.status_code == 201
        assert r2.headers.get("idempotency-replayed") == "true"
        assert r2.headers.get("location") == r1.headers.get("location")
        assert r2.headers.get("etag") == r1.headers.get("etag")

        # X-Request-ID must be present and different
        req_id_2 = r2.headers.get("x-request-id")
        assert req_id_2 is not None
        assert req_id_2 != req_id_1

        data2 = r2.json()
        assert data2 == data1

    # Database verification: exactly one booking, one idempotency key, one outbox event
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        b_count = await session.scalar(
            select(func.count()).select_from(Booking).where(Booking.id == uuid.UUID(booking_id))
        )
        assert b_count == 1

        k_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.key == uuid.UUID(key))
        )
        assert k_count == 1

        o_count = await session.scalar(
            select(func.count())
            .select_from(Outbox)
            .where(Outbox.aggregate_id == uuid.UUID(booking_id))
        )
        assert o_count == 1


@pytest.mark.asyncio
async def test_same_key_different_payload_mismatch() -> None:
    user = await create_user()
    token = make_token(user)
    r1_res = await create_resource()
    r2_res = await create_resource()
    s, e = get_aligned_window(days_ahead=3, hour=10)

    key = str(uuid.uuid4())
    client_ip = f"10.2.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    payload1 = {
        "resource_id": str(r1_res.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    payload2 = {
        "resource_id": str(r2_res.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }

    async with make_client(ip=client_ip) as client:
        # 1. First request succeeds
        res1 = await client.post("/api/v1/bookings", json=payload1, headers=headers)
        assert res1.status_code == 201
        orig_data = res1.json()

        # 2. Second request with different payload -> 422 IDEMPOTENCY_KEY_MISMATCH
        res2 = await client.post("/api/v1/bookings", json=payload2, headers=headers)
        assert res2.status_code == 422
        assert res2.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"

        # 3. Original stored result is unchanged and still replayable
        res3 = await client.post("/api/v1/bookings", json=payload1, headers=headers)
        assert res3.status_code == 201
        assert res3.headers.get("idempotency-replayed") == "true"
        assert res3.json() == orig_data


@pytest.mark.asyncio
async def test_timezone_normalization_replays() -> None:
    user = await create_user()
    token = make_token(user)
    resource = await create_resource()

    key = str(uuid.uuid4())
    client_ip = f"10.3.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"

    s, e = get_aligned_window(days_ahead=3, hour=10)
    tz_neg4 = timezone(timedelta(hours=-4))
    s_edt = s.astimezone(tz_neg4)
    e_edt = e.astimezone(tz_neg4)

    payload_utc = {
        "resource_id": str(resource.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    payload_edt = {
        "resource_id": str(resource.id),
        "starts_at": s_edt.isoformat(),
        "ends_at": e_edt.isoformat(),
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }

    async with make_client(ip=client_ip) as client:
        r1 = await client.post("/api/v1/bookings", json=payload_utc, headers=headers)
        assert r1.status_code == 201
        assert r1.headers.get("idempotency-replayed") == "false"
        booking_id = r1.json()["id"]

        r2 = await client.post("/api/v1/bookings", json=payload_edt, headers=headers)
        assert r2.status_code == 201
        assert r2.headers.get("idempotency-replayed") == "true"
        assert r2.json()["id"] == booking_id

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        b_count = await session.scalar(
            select(func.count()).select_from(Booking).where(Booking.resource_id == resource.id)
        )
        assert b_count == 1


@pytest.mark.asyncio
async def test_replay_after_window_enters_past() -> None:
    user = await create_user()
    token = make_token(user)
    resource = await create_resource()
    key = str(uuid.uuid4())
    client_ip = f"10.4.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }

    # Insert a completed idempotency row whose booking start is now in the past
    past_start = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    past_end = datetime(2025, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
    past_payload = {
        "resource_id": str(resource.id),
        "starts_at": past_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": past_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    b_create = BookingCreate(
        resource_id=resource.id,
        starts_at=past_start,
        ends_at=past_end,
    )
    req_hash = _compute_request_hash("POST", "/api/v1/bookings", b_create)
    stored_booking_id = uuid.uuid4()
    stored_body = {
        "id": str(stored_booking_id),
        "resource_id": str(resource.id),
        "user_id": str(user.id),
        "kind": "reservation",
        "starts_at": past_start.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "ends_at": past_end.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "status": "confirmed",
        "expires_at": None,
        "cancellation_reason": None,
        "version": 1,
        "created_at": "2025-01-01T09:00:00.000000Z",
        "updated_at": "2025-01-01T09:00:00.000000Z",
    }
    stored_headers = {
        "Location": f"/api/v1/bookings/{stored_booking_id}",
        "ETag": '"1"',
    }

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO idempotency_keys (
                        user_id, key, request_hash, response_status,
                        response_body, response_headers, created_at, expires_at
                    )
                    SELECT :user_id, :key, :request_hash, 201, :body, :headers,
                           sampled.at, sampled.at + interval '24 hours'
                    FROM (SELECT clock_timestamp() - interval '1 hour' AS at) sampled
                    """
                ),
                {
                    "user_id": user.id,
                    "key": uuid.UUID(key),
                    "request_hash": req_hash,
                    "body": json.dumps(stored_body),
                    "headers": json.dumps(stored_headers),
                },
            )

    # Replaying with the past window must still replay the stored 201
    async with make_client(ip=client_ip) as client:
        r2 = await client.post("/api/v1/bookings", json=past_payload, headers=headers)
        assert r2.status_code == 201
        assert r2.headers.get("idempotency-replayed") == "true"
        assert r2.json()["id"] == str(stored_booking_id)


@pytest.mark.asyncio
async def test_completed_409_replay_after_capacity_frees() -> None:
    u1 = await create_user()
    u2 = await create_user()
    t1 = make_token(u1)
    t2 = make_token(u2)
    resource = await create_resource()

    s, e = get_aligned_window(days_ahead=3, hour=14)
    payload = {
        "resource_id": str(resource.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    client_ip = f"10.5.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        # User 1 books slot S successfully
        k1 = str(uuid.uuid4())
        r1 = await client.post(
            "/api/v1/bookings",
            json=payload,
            headers={"Authorization": f"Bearer {t1}", "Idempotency-Key": k1},
        )
        assert r1.status_code == 201
        booking_1_id = uuid.UUID(r1.json()["id"])

        # User 2 attempts to book same slot S -> 409 SLOT_CONFLICT
        k2 = str(uuid.uuid4())
        r2 = await client.post(
            "/api/v1/bookings",
            json=payload,
            headers={"Authorization": f"Bearer {t2}", "Idempotency-Key": k2},
        )
        assert r2.status_code == 409
        assert r2.headers.get("idempotency-replayed") == "false"
        assert r2.json()["error"]["code"] == "SLOT_CONFLICT"

        # Now cancel User 1's booking in DB to free capacity
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE bookings SET status = 'cancelled', "
                        "version = version + 1 WHERE id = :id"
                    ),
                    {"id": booking_1_id},
                )

        # User 2 replays with old key k2 -> STILL returns cached 409!
        r2_replay = await client.post(
            "/api/v1/bookings",
            json=payload,
            headers={"Authorization": f"Bearer {t2}", "Idempotency-Key": k2},
        )
        assert r2_replay.status_code == 409
        assert r2_replay.headers.get("idempotency-replayed") == "true"
        assert r2_replay.json()["error"]["code"] == "SLOT_CONFLICT"

        # User 2 uses a NEW key k3 -> 201 Created!
        k3 = str(uuid.uuid4())
        r3 = await client.post(
            "/api/v1/bookings",
            json=payload,
            headers={"Authorization": f"Bearer {t2}", "Idempotency-Key": k3},
        )
        assert r3.status_code == 201
        assert r3.headers.get("idempotency-replayed") == "false"
        assert r3.json()["user_id"] == str(u2.id)


@pytest.mark.asyncio
async def test_exact_24_hour_expiry() -> None:
    user = await create_user()
    token = make_token(user)
    resource = await create_resource()
    s, e = get_aligned_window(days_ahead=4, hour=10)
    payload = {
        "resource_id": str(resource.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    key = str(uuid.uuid4())
    client_ip = f"10.6.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

    sessionmaker = get_sessionmaker()

    async with make_client(ip=client_ip) as client:
        # Initial create -> 201
        r1 = await client.post("/api/v1/bookings", json=payload, headers=headers)
        assert r1.status_code == 201

        # 1. 1 second BEFORE expiry: row still replays
        async with sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE idempotency_keys
                        SET expires_at = sampled.at,
                            created_at = sampled.at - interval '24 hours'
                        FROM (SELECT clock_timestamp() + interval '5 seconds' AS at) sampled
                        WHERE key = :k
                        """
                    ),
                    {"k": uuid.UUID(key)},
                )

        r_before = await client.post("/api/v1/bookings", json=payload, headers=headers)
        assert r_before.status_code == 201
        assert r_before.headers.get("idempotency-replayed") == "true"

        # 2. At / past expiry: row is expired, reusable for a new request
        async with sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE idempotency_keys
                        SET expires_at = sampled.at,
                            created_at = sampled.at - interval '24 hours'
                        FROM (SELECT clock_timestamp() - interval '1 second' AS at) sampled
                        WHERE key = :k
                        """
                    ),
                    {"k": uuid.UUID(key)},
                )

        # New payload for a new request on a different slot
        s2, e2 = get_aligned_window(days_ahead=4, hour=14)
        payload2 = {
            "resource_id": str(resource.id),
            "starts_at": s2.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ends_at": e2.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        r_after = await client.post("/api/v1/bookings", json=payload2, headers=headers)
        assert r_after.status_code == 201
        assert r_after.headers.get("idempotency-replayed") == "false"
        assert r_after.json()["id"] != r1.json()["id"]


@pytest.mark.asyncio
async def test_crash_after_commit_before_response() -> None:
    user = await create_user()
    token = make_token(user)
    resource = await create_resource()
    s, e = get_aligned_window(days_ahead=5, hour=10)
    payload = {
        "resource_id": str(resource.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    key = str(uuid.uuid4())
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

    client_ip_1 = f"10.7.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    client_ip_2 = f"10.8.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"

    # First client completes request (or discarded network response)
    async with make_client(ip=client_ip_1) as client1:
        r1 = await client1.post("/api/v1/bookings", json=payload, headers=headers)
        assert r1.status_code == 201
        data1 = r1.json()

    # Second client / retry sends identical request
    async with make_client(ip=client_ip_2) as client2:
        r2 = await client2.post("/api/v1/bookings", json=payload, headers=headers)
        assert r2.status_code == 201
        assert r2.headers.get("idempotency-replayed") == "true"
        assert r2.json() == data1


@pytest.mark.asyncio
async def test_failed_transaction_leaves_no_incomplete_key() -> None:
    user = await create_user()
    token = make_token(user)
    non_existent_res_id = str(uuid.uuid4())
    s, e = get_aligned_window(days_ahead=5, hour=14)
    payload = {
        "resource_id": non_existent_res_id,
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    key = str(uuid.uuid4())
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}
    client_ip = f"10.9.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"

    async with make_client(ip=client_ip) as client:
        # Non-existent resource returns 404
        r = await client.post("/api/v1/bookings", json=payload, headers=headers)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    # Transaction rolled back: zero rows in idempotency_keys
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        k_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.key == uuid.UUID(key))
        )
        assert k_count == 0


@pytest.mark.asyncio
async def test_concurrent_identical_key() -> None:
    user = await create_user()
    token = make_token(user)
    resource = await create_resource()
    s, e = get_aligned_window(days_ahead=6, hour=10)
    payload = {
        "resource_id": str(resource.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    key = str(uuid.uuid4())
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

    client_ip_1 = f"10.10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    client_ip_2 = f"10.11.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"

    async def send_req(ip: str) -> httpx.Response:
        async with make_client(ip=ip) as client:
            return await client.post("/api/v1/bookings", json=payload, headers=headers)

    r1, r2 = await asyncio.gather(send_req(client_ip_1), send_req(client_ip_2))

    assert r1.status_code == 201
    assert r2.status_code == 201

    replayed_flags = {
        r1.headers.get("idempotency-replayed"),
        r2.headers.get("idempotency-replayed"),
    }
    assert replayed_flags == {"true", "false"}

    assert r1.json()["id"] == r2.json()["id"]

    booking_id = uuid.UUID(r1.json()["id"])
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        b_count = await session.scalar(
            select(func.count()).select_from(Booking).where(Booking.id == booking_id)
        )
        assert b_count == 1

        k_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.key == uuid.UUID(key))
        )
        assert k_count == 1

        o_count = await session.scalar(
            select(func.count()).select_from(Outbox).where(Outbox.aggregate_id == booking_id)
        )
        assert o_count == 1


@pytest.mark.asyncio
async def test_concurrent_same_key_different_payloads() -> None:
    user = await create_user()
    token = make_token(user)
    r1_res = await create_resource()
    r2_res = await create_resource()
    s, e = get_aligned_window(days_ahead=6, hour=14)

    payload1 = {
        "resource_id": str(r1_res.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    payload2 = {
        "resource_id": str(r2_res.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    key = str(uuid.uuid4())
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

    client_ip_1 = f"10.12.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    client_ip_2 = f"10.13.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"

    async def send_req(ip: str, payload: dict[str, Any]) -> httpx.Response:
        async with make_client(ip=ip) as client:
            return await client.post("/api/v1/bookings", json=payload, headers=headers)

    res1, res2 = await asyncio.gather(
        send_req(client_ip_1, payload1),
        send_req(client_ip_2, payload2),
    )

    statuses = {res1.status_code, res2.status_code}
    assert statuses == {201, 422}

    err_resp = res1 if res1.status_code == 422 else res2
    assert err_resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"


@pytest.mark.asyncio
async def test_pending_row_never_observable() -> None:
    user = await create_user()
    token = make_token(user)
    resource = await create_resource()
    s, e = get_aligned_window(days_ahead=7, hour=10)
    key = str(uuid.uuid4())
    payload = {
        "resource_id": str(resource.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}
    client_ip = f"10.14.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"

    # Manually insert a row with response_status IS NULL to simulate an invariant violation
    b_create = BookingCreate(
        resource_id=resource.id,
        starts_at=s,
        ends_at=e,
    )
    req_hash = _compute_request_hash("POST", "/api/v1/bookings", b_create)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO idempotency_keys (
                        user_id, key, request_hash, created_at, expires_at
                    )
                    SELECT :user_id, :key, :request_hash, sampled.at,
                           sampled.at + interval '24 hours'
                    FROM (SELECT clock_timestamp() AS at) sampled
                    """
                ),
                {"user_id": user.id, "key": uuid.UUID(key), "request_hash": req_hash},
            )

    async with make_client(ip=client_ip) as client:
        # A visible row with response_status IS NULL yields 503 RETRYABLE_UNAVAILABLE
        r = await client.post("/api/v1/bookings", json=payload, headers=headers)
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "RETRYABLE_UNAVAILABLE"
        assert r.headers.get("retry-after") == "1"


@pytest.mark.asyncio
async def test_overposting_rejected() -> None:
    user = await create_user()
    token = make_token(user)
    resource = await create_resource()
    s, e = get_aligned_window(days_ahead=7, hour=14)

    overposted_payloads = [
        {"user_id": str(uuid.uuid4())},
        {"status": "confirmed"},
        {"kind": "blackout"},
        {"created_by": str(uuid.uuid4())},
        {"role": "admin"},
    ]

    client_ip = f"10.15.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        for extra in overposted_payloads:
            payload = {
                "resource_id": str(resource.id),
                "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
                **extra,
            }
            key = str(uuid.uuid4())
            headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}

            r = await client.post("/api/v1/bookings", json=payload, headers=headers)
            assert r.status_code == 422
            assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        b_count = await session.scalar(
            select(func.count()).select_from(Booking).where(Booking.resource_id == resource.id)
        )
        assert b_count == 0


@pytest.mark.asyncio
async def test_error_contracts_and_headers() -> None:
    user = await create_user()
    token = make_token(user)
    resource = await create_resource()
    client_ip = f"10.16.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"

    s, e = get_aligned_window(days_ahead=8, hour=10)
    valid_payload = {
        "resource_id": str(resource.id),
        "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    async with make_client(ip=client_ip) as client:
        # 1. Missing Idempotency-Key header -> 422 IDEMPOTENCY_KEY_REQUIRED
        r_missing = await client.post(
            "/api/v1/bookings",
            json=valid_payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_missing.status_code == 422
        assert r_missing.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        # 2. Blank Idempotency-Key header -> 422 IDEMPOTENCY_KEY_REQUIRED
        r_blank = await client.post(
            "/api/v1/bookings",
            json=valid_payload,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "   "},
        )
        assert r_blank.status_code == 422
        assert r_blank.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        # 3. Malformed Idempotency-Key header -> 422 IDEMPOTENCY_KEY_INVALID
        r_malformed = await client.post(
            "/api/v1/bookings",
            json=valid_payload,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "not-a-uuid"},
        )
        assert r_malformed.status_code == 422
        assert r_malformed.json()["error"]["code"] == "IDEMPOTENCY_KEY_INVALID"

        # 4. Non-v4 UUID Idempotency-Key -> 422 IDEMPOTENCY_KEY_INVALID
        r_v1 = await client.post(
            "/api/v1/bookings",
            json=valid_payload,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid1())},
        )
        assert r_v1.status_code == 422
        assert r_v1.json()["error"]["code"] == "IDEMPOTENCY_KEY_INVALID"

        # 5. Invalid window errors (422 INVALID_WINDOW)
        # 5a. Naive timestamp
        r_naive = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": str(resource.id),
                "starts_at": "2026-08-01T10:00:00",
                "ends_at": "2026-08-01T11:00:00",
            },
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        )
        assert r_naive.status_code == 422
        assert r_naive.json()["error"]["code"] == "INVALID_WINDOW"

        # 5b. Unaligned minute (not on 30-min boundary)
        r_unaligned = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": str(resource.id),
                "starts_at": "2026-08-01T10:15:00Z",
                "ends_at": "2026-08-01T11:00:00Z",
            },
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        )
        assert r_unaligned.status_code == 422
        assert r_unaligned.json()["error"]["code"] == "INVALID_WINDOW"

        # 5c. Duration < 30 minutes
        r_short = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": str(resource.id),
                "starts_at": "2026-08-01T10:00:00Z",
                "ends_at": "2026-08-01T10:00:00Z",
            },
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        )
        assert r_short.status_code == 422
        assert r_short.json()["error"]["code"] == "INVALID_WINDOW"

        # 5d. Duration > 4 hours
        r_long = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": str(resource.id),
                "starts_at": "2026-08-01T10:00:00Z",
                "ends_at": "2026-08-01T15:00:00Z",
            },
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        )
        assert r_long.status_code == 422
        assert r_long.json()["error"]["code"] == "INVALID_WINDOW"

        # 6. Resource inactive -> 409 RESOURCE_INACTIVE
        inactive_res = await create_resource(active=False)
        r_inactive = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": str(inactive_res.id),
                "starts_at": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
        )
        assert r_inactive.status_code == 409
        assert r_inactive.json()["error"]["code"] == "RESOURCE_INACTIVE"
