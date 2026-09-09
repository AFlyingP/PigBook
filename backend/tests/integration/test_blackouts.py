import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.admin.models import AuditLog
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
    role: str = "admin",
    enabled: bool = True,
) -> User:
    if email is None:
        email = f"{role}_{uuid.uuid4().hex[:8]}@example.com"
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
    active: bool = True,
    version: int = 1,
) -> Resource:
    if name is None:
        name = f"Resource_{uuid.uuid4().hex[:8]}"
    r = Resource(
        id=uuid.uuid4(),
        name=name,
        description="Room for blackout tests",
        location="Wing C",
        active=active,
        version=version,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(r)
    return r


def make_token(user: User, custom_role: str | None = None) -> str:
    settings = get_settings()
    jwt_secret = settings.JWT_SECRET or "test-jwt-secret-minimum-32-bytes-long-12345678"
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user.id),
        "role": custom_role if custom_role is not None else user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "jti": str(uuid.uuid4()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
    }
    return jwt.encode(claims, jwt_secret, algorithm="HS256")


def make_client() -> httpx.AsyncClient:
    client_ip = f"10.9.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(client_ip, 12345)),
        base_url="http://localhost:5173",
    )


@pytest.mark.asyncio
async def test_e21_list_resource_blackouts() -> None:
    admin = await create_user(role="admin")
    member = await create_user(role="member")
    admin_token = make_token(admin)
    resource = await create_resource()

    base_time = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(
        days=2
    )

    # Insert 1 blackout and 1 reservation on the resource
    sessionmaker = get_sessionmaker()
    blackout_row = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=None,
        created_by=admin.id,
        kind="blackout",
        time_range=Range(base_time, base_time + timedelta(hours=2), bounds="[)"),
        status="confirmed",
        version=1,
    )
    reservation_row = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=member.id,
        created_by=member.id,
        kind="reservation",
        time_range=Range(
            base_time + timedelta(hours=3), base_time + timedelta(hours=4), bounds="[)"
        ),
        status="confirmed",
        version=1,
    )
    async with sessionmaker() as session:
        async with session.begin():
            session.add(blackout_row)
            session.add(reservation_row)

    async with make_client() as client:
        headers = {"Authorization": f"Bearer {admin_token}"}
        res = await client.get(f"/api/v1/admin/resources/{resource.id}/blackouts", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 1
        items = data["items"]
        assert len(items) == 1
        assert items[0]["id"] == str(blackout_row.id)
        assert items[0]["kind"] == "blackout"
        assert items[0]["user_id"] is None

        # Missing resource -> 404
        bad_id = uuid.uuid4()
        res_missing = await client.get(
            f"/api/v1/admin/resources/{bad_id}/blackouts", headers=headers
        )
        assert res_missing.status_code == 404


@pytest.mark.asyncio
async def test_e22_create_blackout_success_and_invariants() -> None:
    admin = await create_user(role="admin")
    admin_token = make_token(admin)
    resource = await create_resource()

    slot_start = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=2)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    slot_end = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=2, hours=3)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    idemp_key = str(uuid.uuid4())
    body = {"starts_at": slot_start, "ends_at": slot_end}

    async with make_client() as client:
        headers = {
            "Authorization": f"Bearer {admin_token}",
            "Idempotency-Key": idemp_key,
        }
        res = await client.post(
            f"/api/v1/admin/resources/{resource.id}/blackouts",
            json=body,
            headers=headers,
        )
        assert res.status_code == 201
        data = res.json()
        blackout_id = data["id"]
        assert data["kind"] == "blackout"
        assert data["user_id"] is None
        assert data["status"] == "confirmed"
        assert data["version"] == 1
        assert res.headers.get("Location") == f"/api/v1/admin/blackouts/{blackout_id}"
        assert res.headers.get("ETag") == '"1"'
        assert res.headers.get("Idempotency-Replayed") == "false"

        # Verify NO notification outbox event for blackout
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt_outbox = select(func.count(Outbox.id)).where(
                Outbox.aggregate_id == uuid.UUID(blackout_id)
            )
            outbox_count = (await session.execute(stmt_outbox)).scalar_one()
            assert outbox_count == 0, "Blackout creation must not emit an outbox notification event"

            # Verify audit log exists
            stmt_audit = select(AuditLog).where(
                AuditLog.target_type == "booking",
                AuditLog.target_id == uuid.UUID(blackout_id),
            )
            audit_entry = (await session.execute(stmt_audit)).scalar_one()
            assert audit_entry.action == "admin.blackout_create"
            assert audit_entry.actor_id == admin.id
            assert audit_entry.details["resource_id"] == str(resource.id)

        # Replay with same key and body -> returns stored response with Idempotency-Replayed: true
        res_replay = await client.post(
            f"/api/v1/admin/resources/{resource.id}/blackouts",
            json=body,
            headers=headers,
        )
        assert res_replay.status_code == 201
        assert res_replay.headers.get("Idempotency-Replayed") == "true"
        assert res_replay.json()["id"] == blackout_id


@pytest.mark.asyncio
async def test_e22_cross_endpoint_and_payload_key_mismatch() -> None:
    admin = await create_user(role="admin")
    admin_token = make_token(admin)
    resource = await create_resource()

    slot_start = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=2)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    slot_end = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=2, hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    idemp_key = str(uuid.uuid4())
    body = {"starts_at": slot_start, "ends_at": slot_end}

    async with make_client() as client:
        headers = {
            "Authorization": f"Bearer {admin_token}",
            "Idempotency-Key": idemp_key,
        }
        res = await client.post(
            f"/api/v1/admin/resources/{resource.id}/blackouts",
            json=body,
            headers=headers,
        )
        assert res.status_code == 201

        # 1. Reuse key with different payload on same blackout endpoint -> 422
        diff_body = {
            "starts_at": slot_start,
            "ends_at": (
                datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
                + timedelta(days=2, hours=3)
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        res_mismatch = await client.post(
            f"/api/v1/admin/resources/{resource.id}/blackouts",
            json=diff_body,
            headers=headers,
        )
        assert res_mismatch.status_code == 422
        assert res_mismatch.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"

        # 2. Cross-endpoint key reuse on /api/v1/bookings -> 422 IDEMPOTENCY_KEY_MISMATCH
        bkg_body = {
            "resource_id": str(resource.id),
            "starts_at": slot_start,
            "ends_at": slot_end,
        }
        res_cross = await client.post(
            "/api/v1/bookings",
            json=bkg_body,
            headers=headers,
        )
        assert res_cross.status_code == 422
        assert res_cross.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"


@pytest.mark.asyncio
async def test_e22_blackout_conflict_and_inactive_resource() -> None:
    admin = await create_user(role="admin")
    admin_token = make_token(admin)
    inactive_res = await create_resource(active=False)
    active_res = await create_resource(active=True)

    t_start = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=2)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_end = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=2, hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    async with make_client() as client:
        # Inactive resource -> cached 409 RESOURCE_INACTIVE
        h_inact = {
            "Authorization": f"Bearer {admin_token}",
            "Idempotency-Key": str(uuid.uuid4()),
        }
        res_inact = await client.post(
            f"/api/v1/admin/resources/{inactive_res.id}/blackouts",
            json={"starts_at": t_start, "ends_at": t_end},
            headers=h_inact,
        )
        assert res_inact.status_code == 409
        assert res_inact.json()["error"]["code"] == "RESOURCE_INACTIVE"

        # Missing resource -> uncached 404
        res_missing = await client.post(
            f"/api/v1/admin/resources/{uuid.uuid4()}/blackouts",
            json={"starts_at": t_start, "ends_at": t_end},
            headers={
                "Authorization": f"Bearer {admin_token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        assert res_missing.status_code == 404

        # Successfully create first blackout on active_res
        h_ok = {
            "Authorization": f"Bearer {admin_token}",
            "Idempotency-Key": str(uuid.uuid4()),
        }
        res_ok = await client.post(
            f"/api/v1/admin/resources/{active_res.id}/blackouts",
            json={"starts_at": t_start, "ends_at": t_end},
            headers=h_ok,
        )
        assert res_ok.status_code == 201

        # Second blackout overlapping -> 409 SLOT_CONFLICT
        h_conflict = {
            "Authorization": f"Bearer {admin_token}",
            "Idempotency-Key": str(uuid.uuid4()),
        }
        res_conflict = await client.post(
            f"/api/v1/admin/resources/{active_res.id}/blackouts",
            json={"starts_at": t_start, "ends_at": t_end},
            headers=h_conflict,
        )
        assert res_conflict.status_code == 409
        assert res_conflict.json()["error"]["code"] == "SLOT_CONFLICT"


@pytest.mark.asyncio
async def test_e22_concurrent_blackout_vs_reservation_race() -> None:
    admin = await create_user(role="admin")
    member = await create_user(role="member")
    resource = await create_resource()

    slot_start = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=3)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    slot_end = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=3, hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    async with make_client() as c_admin, make_client() as c_member:
        blackout_req = c_admin.post(
            f"/api/v1/admin/resources/{resource.id}/blackouts",
            json={"starts_at": slot_start, "ends_at": slot_end},
            headers={
                "Authorization": f"Bearer {make_token(admin)}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        booking_req = c_member.post(
            "/api/v1/bookings",
            json={"resource_id": str(resource.id), "starts_at": slot_start, "ends_at": slot_end},
            headers={
                "Authorization": f"Bearer {make_token(member)}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )

        res_blackout, res_booking = await asyncio.gather(blackout_req, booking_req)

        statuses = sorted([res_blackout.status_code, res_booking.status_code])
        assert statuses == [201, 409], f"Expected exactly one 201 and one 409, got {statuses}"
        if res_blackout.status_code == 409:
            assert res_blackout.json()["error"]["code"] == "SLOT_CONFLICT"
        if res_booking.status_code == 409:
            assert res_booking.json()["error"]["code"] == "SLOT_CONFLICT"


@pytest.mark.asyncio
async def test_e23_cancel_blackout_promotes_waiters_and_no_email() -> None:
    admin = await create_user(role="admin")
    member = await create_user(role="member")
    admin_token = make_token(admin)
    resource = await create_resource()

    base_time = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(
        days=2
    )
    end_time = base_time + timedelta(hours=2)

    sessionmaker = get_sessionmaker()
    blackout_row = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=None,
        created_by=admin.id,
        kind="blackout",
        time_range=Range(base_time, end_time, bounds="[)"),
        status="confirmed",
        version=1,
    )
    # Add a waiter desiring the exact same interval
    waiter = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=member.id,
        resource_id=resource.id,
        time_range=Range(base_time, end_time, bounds="[)"),
        status="waiting",
        offered_booking_id=None,
        version=1,
    )

    async with sessionmaker() as session:
        async with session.begin():
            session.add(blackout_row)
            session.add(waiter)

    async with make_client() as client:
        headers = {"Authorization": f"Bearer {admin_token}"}

        # 1. Missing If-Match -> 428
        res_no_if = await client.delete(
            f"/api/v1/admin/blackouts/{blackout_row.id}", headers=headers
        )
        assert res_no_if.status_code == 428

        # 2. Stale If-Match -> 412
        res_stale = await client.delete(
            f"/api/v1/admin/blackouts/{blackout_row.id}",
            headers={**headers, "If-Match": '"99"'},
        )
        assert res_stale.status_code == 412

        # 3. Successful cancellation
        res_ok = await client.delete(
            f"/api/v1/admin/blackouts/{blackout_row.id}",
            headers={**headers, "If-Match": '"1"'},
        )
        assert res_ok.status_code == 200
        data = res_ok.json()
        assert data["status"] == "cancelled"
        assert data["version"] == 2
        assert res_ok.headers.get("ETag") == '"2"'

        # Verify DB state: blackout cancelled, waiter promoted to offered, NO outbox event
        async with sessionmaker() as session:
            stmt_b = select(Booking).where(Booking.id == blackout_row.id)
            b_db = (await session.execute(stmt_b)).scalar_one()
            assert b_db.status == "cancelled"
            assert b_db.version == 2

            # Check audit log
            stmt_audit = select(AuditLog).where(
                AuditLog.target_type == "booking",
                AuditLog.target_id == blackout_row.id,
                AuditLog.action == "admin.blackout_cancel",
            )
            audit_entry = (await session.execute(stmt_audit)).scalar_one()
            assert audit_entry.actor_id == admin.id

            # Verify no outbox cancellation email for blackout
            stmt_outbox = select(func.count(Outbox.id)).where(
                Outbox.aggregate_id == blackout_row.id,
                Outbox.event_type == "booking_cancelled",
            )
            assert (await session.execute(stmt_outbox)).scalar_one() == 0

            # Verify waitlist promotion occurred atomically
            stmt_w = select(WaitlistEntry).where(WaitlistEntry.id == waiter.id)
            w_db = (await session.execute(stmt_w)).scalar_one()
            assert w_db.status == "offered"
            assert w_db.offered_booking_id is not None

        # 4. Duplicate cancellation at current version -> 200 without error
        res_dup = await client.delete(
            f"/api/v1/admin/blackouts/{blackout_row.id}",
            headers={**headers, "If-Match": '"2"'},
        )
        assert res_dup.status_code == 200
        assert res_dup.json()["status"] == "cancelled"
        assert res_dup.json()["version"] == 2


@pytest.mark.asyncio
async def test_e23_cannot_cancel_completed_blackout() -> None:
    admin = await create_user(role="admin")
    admin_token = make_token(admin)
    resource = await create_resource()

    past_start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(
        days=2
    )
    past_end = past_start + timedelta(hours=2)

    sessionmaker = get_sessionmaker()
    past_blackout = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=None,
        created_by=admin.id,
        kind="blackout",
        time_range=Range(past_start, past_end, bounds="[)"),
        status="confirmed",
        version=1,
    )
    async with sessionmaker() as session:
        async with session.begin():
            session.add(past_blackout)

    async with make_client() as client:
        headers = {"Authorization": f"Bearer {admin_token}", "If-Match": '"1"'}
        res = await client.delete(f"/api/v1/admin/blackouts/{past_blackout.id}", headers=headers)
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "TOO_LATE"
