import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from sqlalchemy import select
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
    display_name: str = "Test Admin",
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
    description: str = "Test Resource Description",
    location: str = "Room 101",
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
async def test_e17_list_admin_resources_pagination_and_active_filter() -> None:
    admin = await create_user(role="admin")
    admin_token = make_token(admin)

    # Create distinct resources: 2 active, 1 inactive
    prefix = f"A_{uuid.uuid4().hex[:6]}"
    r1 = await create_resource(name=f"{prefix}_Alpha", active=True)
    r2 = await create_resource(name=f"{prefix}_Beta", active=False)
    r3 = await create_resource(name=f"{prefix}_Gamma", active=True)

    async with make_client() as client:
        headers = {"Authorization": f"Bearer {admin_token}"}

        # 1. Unfiltered: returns both active and inactive
        res = await client.get("/api/v1/admin/resources?limit=100&offset=0", headers=headers)
        assert res.status_code == 200
        data = res.json()
        ids = [item["id"] for item in data["items"]]
        assert str(r1.id) in ids
        assert str(r2.id) in ids
        assert str(r3.id) in ids

        # Verify ordering: name ascending, then id ascending
        filtered_items = [
            item for item in data["items"] if item["id"] in [str(r1.id), str(r2.id), str(r3.id)]
        ]
        names = [item["name"] for item in filtered_items]
        assert names == [f"{prefix}_Alpha", f"{prefix}_Beta", f"{prefix}_Gamma"]

        # 2. Filter active=true
        res_act = await client.get("/api/v1/admin/resources?active=true&limit=100", headers=headers)
        assert res_act.status_code == 200
        data_act = res_act.json()
        act_ids = [item["id"] for item in data_act["items"]]
        assert str(r1.id) in act_ids
        assert str(r3.id) in act_ids
        assert str(r2.id) not in act_ids

        # 3. Filter active=false
        res_inact = await client.get(
            "/api/v1/admin/resources?active=false&limit=100", headers=headers
        )
        assert res_inact.status_code == 200
        data_inact = res_inact.json()
        inact_ids = [item["id"] for item in data_inact["items"]]
        assert str(r2.id) in inact_ids
        assert str(r1.id) not in inact_ids
        assert str(r3.id) not in inact_ids

        # 4. Pagination bounds
        res_page = await client.get("/api/v1/admin/resources?limit=1&offset=0", headers=headers)
        assert res_page.status_code == 200
        assert len(res_page.json()["items"]) == 1
        assert res_page.json()["limit"] == 1
        assert res_page.json()["offset"] == 0


@pytest.mark.asyncio
async def test_e18_create_resource_success_and_audit() -> None:
    admin = await create_user(role="admin")
    admin_token = make_token(admin)

    body = {
        "name": f"Studio_{uuid.uuid4().hex[:6]}",
        "description": "Soundproof podcast studio",
        "location": "Building B Floor 2",
    }

    async with make_client() as client:
        headers = {"Authorization": f"Bearer {admin_token}"}
        res = await client.post("/api/v1/admin/resources", json=body, headers=headers)
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == body["name"]
        assert data["description"] == body["description"]
        assert data["location"] == body["location"]
        assert data["active"] is True
        assert data["version"] == 1
        assert "created_at" in data
        assert "updated_at" in data

        res_id = data["id"]
        assert res.headers.get("Location") == f"/api/v1/admin/resources/{res_id}"
        assert res.headers.get("ETag") == '"1"'
        assert "X-Request-ID" in res.headers

        # Verify audit log in DB
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = select(AuditLog).where(
                AuditLog.target_type == "resource",
                AuditLog.target_id == uuid.UUID(res_id),
            )
            audit_entry = (await session.execute(stmt)).scalar_one()
            assert audit_entry.action == "admin.resource_create"
            assert audit_entry.actor_id == admin.id
            assert audit_entry.details["name"] == body["name"]
            assert audit_entry.details["location"] == body["location"]


@pytest.mark.asyncio
async def test_e18_create_resource_validation_failures() -> None:
    admin = await create_user(role="admin")
    admin_token = make_token(admin)

    async with make_client() as client:
        headers = {"Authorization": f"Bearer {admin_token}"}

        # Overposting extra field -> 422
        bad_extra = {
            "name": "Room Extra",
            "location": "Loc",
            "active": False,
        }
        res_extra = await client.post("/api/v1/admin/resources", json=bad_extra, headers=headers)
        assert res_extra.status_code == 422
        assert res_extra.json()["error"]["code"] == "VALIDATION_ERROR"

        # Missing location -> 422
        bad_missing = {"name": "Room Only"}
        res_missing = await client.post(
            "/api/v1/admin/resources", json=bad_missing, headers=headers
        )
        assert res_missing.status_code == 422


@pytest.mark.asyncio
async def test_e19_patch_resource_guarded_optimistic_locking() -> None:
    admin = await create_user(role="admin")
    admin_token = make_token(admin)
    resource = await create_resource(name="Original Name", location="Original Loc", version=1)

    async with make_client() as client:
        headers = {"Authorization": f"Bearer {admin_token}"}

        # 1. Missing If-Match -> 428 PRECONDITION_REQUIRED
        res_no_if_match = await client.patch(
            f"/api/v1/admin/resources/{resource.id}",
            json={"name": "New Name"},
            headers=headers,
        )
        assert res_no_if_match.status_code == 428
        assert res_no_if_match.json()["error"]["code"] == "PRECONDITION_REQUIRED"

        # 2. Malformed If-Match -> 422 VALIDATION_ERROR
        res_bad_if_match = await client.patch(
            f"/api/v1/admin/resources/{resource.id}",
            json={"name": "New Name"},
            headers={**headers, "If-Match": "invalid-etag"},
        )
        assert res_bad_if_match.status_code == 422
        assert res_bad_if_match.json()["error"]["code"] == "VALIDATION_ERROR"

        # 3. Stale If-Match -> 412 VERSION_MISMATCH
        res_stale = await client.patch(
            f"/api/v1/admin/resources/{resource.id}",
            json={"name": "New Name"},
            headers={**headers, "If-Match": '"99"'},
        )
        assert res_stale.status_code == 412
        assert res_stale.json()["error"]["code"] == "VERSION_MISMATCH"

        # 4. Null field validation failure -> 422
        res_null = await client.patch(
            f"/api/v1/admin/resources/{resource.id}",
            json={"name": None},
            headers={**headers, "If-Match": '"1"'},
        )
        assert res_null.status_code == 422

        # 5. Empty body -> 422
        res_empty = await client.patch(
            f"/api/v1/admin/resources/{resource.id}",
            json={},
            headers={**headers, "If-Match": '"1"'},
        )
        assert res_empty.status_code == 422

        # 6. Successful patch
        res_ok = await client.patch(
            f"/api/v1/admin/resources/{resource.id}",
            json={"name": "Renamed Room", "location": "Floor 3"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert res_ok.status_code == 200
        data = res_ok.json()
        assert data["name"] == "Renamed Room"
        assert data["location"] == "Floor 3"
        assert data["version"] == 2
        assert res_ok.headers.get("ETag") == '"2"'

        # Verify audit log
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = select(AuditLog).where(
                AuditLog.target_type == "resource",
                AuditLog.target_id == resource.id,
                AuditLog.action == "admin.resource_patch",
            )
            audit_entry = (await session.execute(stmt)).scalar_one()
            assert audit_entry.details["name"] == "Renamed Room"
            assert audit_entry.details["location"] == "Floor 3"


@pytest.mark.asyncio
async def test_e19_patch_resource_same_version_race() -> None:
    admin = await create_user(role="admin")
    admin_token = make_token(admin)
    resource = await create_resource(name="Race Room", version=1)

    # Two concurrent same-version patches produce one 200 and one 412
    async with make_client() as c1, make_client() as c2:
        headers = {"Authorization": f"Bearer {admin_token}", "If-Match": '"1"'}

        req1 = c1.patch(
            f"/api/v1/admin/resources/{resource.id}",
            json={"name": "Winner 1"},
            headers=headers,
        )
        req2 = c2.patch(
            f"/api/v1/admin/resources/{resource.id}",
            json={"name": "Winner 2"},
            headers=headers,
        )

        res1, res2 = await asyncio.gather(req1, req2)
        statuses = sorted([res1.status_code, res2.status_code])
        assert statuses == [200, 412], f"Expected [200, 412], got {statuses}"


@pytest.mark.asyncio
async def test_e19_and_e20_reject_when_resource_in_use() -> None:
    admin = await create_user(role="admin")
    admin_token = make_token(admin)
    member = await create_user(role="member")
    resource = await create_resource(name="In-Use Room", version=1)

    future_start = datetime.now(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    ) + timedelta(days=2)
    future_end = future_start + timedelta(hours=2)

    # Insert confirmed booking in the future
    sessionmaker = get_sessionmaker()
    booking = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=member.id,
        created_by=member.id,
        kind="reservation",
        time_range=Range(future_start, future_end, bounds="[)"),
        status="confirmed",
        expires_at=None,
        version=1,
    )
    async with sessionmaker() as session:
        async with session.begin():
            session.add(booking)

    async with make_client() as client:
        headers = {"Authorization": f"Bearer {admin_token}"}

        # 1. Stale version precedence over state check on DELETE -> 412 before 409
        res_stale = await client.delete(
            f"/api/v1/admin/resources/{resource.id}",
            headers={**headers, "If-Match": '"99"'},
        )
        assert res_stale.status_code == 412
        assert res_stale.json()["error"]["code"] == "VERSION_MISMATCH"

        # 2. DELETE with current version rejected with 409 RESOURCE_IN_USE
        res_del = await client.delete(
            f"/api/v1/admin/resources/{resource.id}",
            headers={**headers, "If-Match": '"1"'},
        )
        assert res_del.status_code == 409
        assert res_del.json()["error"]["code"] == "RESOURCE_IN_USE"

        # State check: resource must remain active=True, version=1
        async with sessionmaker() as session:
            stmt = select(Resource).where(Resource.id == resource.id)
            r_db = (await session.execute(stmt)).scalar_one()
            assert r_db.active is True
            assert r_db.version == 1

        # 3. PATCH active=False also rejected with 409 RESOURCE_IN_USE
        res_patch = await client.patch(
            f"/api/v1/admin/resources/{resource.id}",
            json={"active": False},
            headers={**headers, "If-Match": '"1"'},
        )
        assert res_patch.status_code == 409
        assert res_patch.json()["error"]["code"] == "RESOURCE_IN_USE"


@pytest.mark.asyncio
async def test_e20_reject_when_waitlist_entry_exists() -> None:
    admin = await create_user(role="admin")
    admin_token = make_token(admin)
    member = await create_user(role="member")
    resource = await create_resource(name="Waitlisted Room", version=1)

    future_start = datetime.now(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    ) + timedelta(days=2)
    future_end = future_start + timedelta(hours=2)

    # Insert waiting waitlist entry
    sessionmaker = get_sessionmaker()
    w_entry = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=member.id,
        resource_id=resource.id,
        time_range=Range(future_start, future_end, bounds="[)"),
        status="waiting",
        offered_booking_id=None,
        version=1,
    )
    async with sessionmaker() as session:
        async with session.begin():
            session.add(w_entry)

    async with make_client() as client:
        headers = {"Authorization": f"Bearer {admin_token}", "If-Match": '"1"'}
        res = await client.delete(f"/api/v1/admin/resources/{resource.id}", headers=headers)
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "RESOURCE_IN_USE"


@pytest.mark.asyncio
async def test_e20_archive_resource_success_and_audit() -> None:
    admin = await create_user(role="admin")
    admin_token = make_token(admin)
    member = await create_user(role="member")
    resource = await create_resource(name="Archivable Room", version=1)

    # Add a PAST completed booking (upper(range) < db_now) to ensure it does not block archival
    past_start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(
        days=5
    )
    past_end = past_start + timedelta(hours=2)

    sessionmaker = get_sessionmaker()
    past_booking = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=member.id,
        created_by=member.id,
        kind="reservation",
        time_range=Range(past_start, past_end, bounds="[)"),
        status="confirmed",
        expires_at=None,
        version=1,
    )
    async with sessionmaker() as session:
        async with session.begin():
            session.add(past_booking)

    async with make_client() as client:
        headers = {"Authorization": f"Bearer {admin_token}", "If-Match": '"1"'}
        res = await client.delete(f"/api/v1/admin/resources/{resource.id}", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["active"] is False
        assert data["version"] == 2
        assert res.headers.get("ETag") == '"2"'

        # Verify audit log in DB
        async with sessionmaker() as session:
            stmt = select(AuditLog).where(
                AuditLog.target_type == "resource",
                AuditLog.target_id == resource.id,
                AuditLog.action == "admin.resource_archive",
            )
            audit_entry = (await session.execute(stmt)).scalar_one()
            assert audit_entry.details == {"active": False}
            assert audit_entry.actor_id == admin.id


@pytest.mark.asyncio
async def test_e20_archive_vs_booking_create_race() -> None:
    admin = await create_user(role="admin")
    member = await create_user(role="member")
    admin_token = make_token(admin)
    member_token = make_token(member)

    resource = await create_resource(name="Contested Archive Room", version=1)

    slot_start = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=3)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    slot_end = (
        datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        + timedelta(days=3, hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    async with make_client() as c_admin, make_client() as c_member:
        archive_req = c_admin.delete(
            f"/api/v1/admin/resources/{resource.id}",
            headers={"Authorization": f"Bearer {admin_token}", "If-Match": '"1"'},
        )
        booking_req = c_member.post(
            "/api/v1/bookings",
            json={
                "resource_id": str(resource.id),
                "starts_at": slot_start,
                "ends_at": slot_end,
            },
            headers={
                "Authorization": f"Bearer {member_token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )

        res_archive, res_booking = await asyncio.gather(archive_req, booking_req)

        # Serial outcomes:
        # Case A: Archive committed first -> archive=200, booking=409 (RESOURCE_INACTIVE)
        # Case B: Booking committed first -> booking=201, archive=409 (RESOURCE_IN_USE)
        if res_archive.status_code == 200:
            assert res_booking.status_code == 409
            assert res_booking.json()["error"]["code"] == "RESOURCE_INACTIVE"
        else:
            assert res_archive.status_code == 409
            assert res_archive.json()["error"]["code"] == "RESOURCE_IN_USE"
            assert res_booking.status_code == 201


@pytest.mark.asyncio
async def test_admin_resources_permissions_and_role_spoofing() -> None:
    member = await create_user(role="member")
    disabled = await create_user(role="member", enabled=False)
    resource = await create_resource(name="Perm Room", version=1)

    member_token = make_token(member)
    disabled_token = make_token(disabled)
    spoofed_token = make_token(member, custom_role="admin")  # JWT claims admin, DB has member

    async with make_client() as client:
        # 1. Anonymous -> 401 AUTH_REQUIRED
        assert (await client.get("/api/v1/admin/resources")).status_code == 401
        assert (
            await client.post("/api/v1/admin/resources", json={"name": "X", "location": "Y"})
        ).status_code == 401
        assert (
            await client.patch(
                f"/api/v1/admin/resources/{resource.id}",
                json={"name": "X"},
                headers={"If-Match": '"1"'},
            )
        ).status_code == 401
        assert (
            await client.delete(
                f"/api/v1/admin/resources/{resource.id}", headers={"If-Match": '"1"'}
            )
        ).status_code == 401

        # 2. Disabled user -> 401 INVALID_TOKEN
        h_dis = {"Authorization": f"Bearer {disabled_token}"}
        assert (await client.get("/api/v1/admin/resources", headers=h_dis)).status_code == 401
        assert (
            await client.post(
                "/api/v1/admin/resources", json={"name": "X", "location": "Y"}, headers=h_dis
            )
        ).status_code == 401
        assert (
            await client.patch(
                f"/api/v1/admin/resources/{resource.id}",
                json={"name": "X"},
                headers={**h_dis, "If-Match": '"1"'},
            )
        ).status_code == 401
        assert (
            await client.delete(
                f"/api/v1/admin/resources/{resource.id}", headers={**h_dis, "If-Match": '"1"'}
            )
        ).status_code == 401

        # 3. Normal member -> 403 FORBIDDEN
        h_mem = {"Authorization": f"Bearer {member_token}"}
        assert (await client.get("/api/v1/admin/resources", headers=h_mem)).status_code == 403
        assert (
            await client.post(
                "/api/v1/admin/resources", json={"name": "X", "location": "Y"}, headers=h_mem
            )
        ).status_code == 403
        assert (
            await client.patch(
                f"/api/v1/admin/resources/{resource.id}",
                json={"name": "X"},
                headers={**h_mem, "If-Match": '"1"'},
            )
        ).status_code == 403
        assert (
            await client.delete(
                f"/api/v1/admin/resources/{resource.id}", headers={**h_mem, "If-Match": '"1"'}
            )
        ).status_code == 403

        # 4. Spoofed JWT -> 403 FORBIDDEN (DB role is authoritative)
        h_spoof = {"Authorization": f"Bearer {spoofed_token}"}
        assert (await client.get("/api/v1/admin/resources", headers=h_spoof)).status_code == 403
        assert (
            await client.post(
                "/api/v1/admin/resources", json={"name": "X", "location": "Y"}, headers=h_spoof
            )
        ).status_code == 403
        assert (
            await client.patch(
                f"/api/v1/admin/resources/{resource.id}",
                json={"name": "X"},
                headers={**h_spoof, "If-Match": '"1"'},
            )
        ).status_code == 403
        assert (
            await client.delete(
                f"/api/v1/admin/resources/{resource.id}", headers={**h_spoof, "If-Match": '"1"'}
            )
        ).status_code == 403
