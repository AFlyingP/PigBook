import base64
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")
os.environ.setdefault("METRICS_TOKEN", "test-metrics-token-minimum-32-bytes-long-1234")

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range

from app.auth.dependencies import Policy, policy_registry
from app.auth.models import RefreshToken, User
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


async def create_refresh_token(user: User, now: datetime) -> str:
    raw_bytes = os.urandom(32)
    raw_refresh = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
    token_hash = hashlib.sha256(raw_refresh.encode("utf-8")).hexdigest()
    family_id = uuid.uuid4()
    rt = RefreshToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=token_hash,
        family_id=family_id,
        parent_id=None,
        created_at=now,
        expires_at=now + timedelta(days=7),
        family_expires_at=now + timedelta(days=30),
        used_at=None,
        revoked_at=None,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(rt)
    return raw_refresh


async def create_user(
    *,
    email: str | None = None,
    password: str = "valid-password-123",
    role: str = "member",
    enabled: bool = True,
) -> User:
    if email is None:
        email = f"perm_{uuid.uuid4().hex[:8]}@example.com"

    pwd_hash = hash_password(password)
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=pwd_hash,
        display_name="Perm User",
        role=role,
        enabled=enabled,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(user)
    return user


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


def make_client(ip: str = "127.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 12345)),
        base_url="http://localhost:5173",
    )


def test_policy_metadata_coverage() -> None:
    """Every registered route has policy metadata in policy_registry."""
    assert set(policy_registry.keys()) == {
        "E01",
        "E02",
        "E03",
        "E04",
        "E05",
        "E06",
        "E07",
        "E08",
        "E09",
        "E10",
        "E11",
        "E12",
        "E13",
        "E14",
        "E15",
        "E16",
        "E17",
        "E18",
        "E19",
        "E20",
        "E21",
        "E22",
        "E23",
        "E24",
        "E25",
        "E26",
        "E27",
        "E28",
        "E29",
        "E30",
        "E31",
        "E32",
        "E33",
        "E34",
        "E35",
        "E36",
        "E37",
    }
    assert policy_registry["E01"] == Policy.public
    assert policy_registry["E02"] == Policy.public
    assert policy_registry["E03"] == Policy.public
    assert policy_registry["E04"] == Policy.public
    assert policy_registry["E05"] == Policy.authenticated
    assert policy_registry["E06"] == Policy.authenticated
    assert policy_registry["E07"] == Policy.authenticated
    assert policy_registry["E08"] == Policy.authenticated
    assert policy_registry["E09"] == Policy.authenticated
    assert policy_registry["E10"] == Policy.own_booking
    assert policy_registry["E11"] == Policy.own_booking
    assert policy_registry["E12"] == Policy.own_booking
    assert policy_registry["E13"] == Policy.authenticated
    assert policy_registry["E14"] == Policy.own_waitlist
    assert policy_registry["E15"] == Policy.own_waitlist
    assert policy_registry["E16"] == Policy.own_waitlist
    assert policy_registry["E17"] == Policy.admin
    assert policy_registry["E18"] == Policy.admin
    assert policy_registry["E19"] == Policy.admin
    assert policy_registry["E20"] == Policy.admin
    assert policy_registry["E21"] == Policy.admin
    assert policy_registry["E22"] == Policy.admin
    assert policy_registry["E23"] == Policy.admin
    assert policy_registry["E24"] == Policy.admin
    assert policy_registry["E25"] == Policy.admin
    assert policy_registry["E26"] == Policy.admin
    assert policy_registry["E27"] == Policy.admin
    assert policy_registry["E28"] == Policy.admin
    assert policy_registry["E29"] == Policy.admin
    assert policy_registry["E30"] == Policy.admin
    assert policy_registry["E31"] == Policy.admin
    assert policy_registry["E32"] == Policy.admin
    assert policy_registry["E33"] == Policy.authenticated
    assert policy_registry["E34"] == Policy.admin
    assert policy_registry["E35"] == Policy.public
    assert policy_registry["E36"] == Policy.public
    assert policy_registry["E37"] == Policy.metrics


@pytest.mark.asyncio
async def test_permission_matrix() -> None:
    """Exercise matrix of personas (anonymous, member, admin, disabled)

    across all registered endpoints (E02, E05, E35).
    Unauthorized requests must be OTHERWISE VALID (send well-formed body).
    """
    password = "valid-password-123"
    member_user = await create_user(password=password, role="member", enabled=True)
    admin_user = await create_user(password=password, role="admin", enabled=True)
    disabled_user = await create_user(password=password, role="member", enabled=False)

    member_token = make_token(member_user)
    admin_token = make_token(admin_user)
    disabled_token = make_token(disabled_user)

    client_ip = f"10.9.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"

    async with make_client(ip=client_ip) as client:
        # 1. E35: GET /healthz (Policy.public) -> Allowed for all personas
        for token_val in [None, member_token, admin_token, disabled_token]:
            headers = {"Authorization": f"Bearer {token_val}"} if token_val else {}
            r = await client.get("/healthz", headers=headers)
            assert r.status_code == 200
            assert r.json()["status"] == "ok"

        # E36: GET /readyz (Policy.public) -> Allowed for all personas
        for token_val in [None, member_token, admin_token, disabled_token]:
            headers = {"Authorization": f"Bearer {token_val}"} if token_val else {}
            r = await client.get("/readyz", headers=headers)
            assert r.status_code == 200
            assert r.json()["status"] == "ready"

        # E37: GET /metrics (Policy.metrics) -> Requires valid METRICS_TOKEN
        valid_metrics_token = os.environ.get(
            "METRICS_TOKEN", "test-metrics-token-minimum-32-bytes-long-1234"
        )

        # Anonymous caller without token -> 401
        r_m_anon = await client.get("/metrics")
        assert r_m_anon.status_code == 401
        assert r_m_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        # Member caller with user JWT -> 401
        r_m_mem = await client.get("/metrics", headers={"Authorization": f"Bearer {member_token}"})
        assert r_m_mem.status_code == 401
        assert r_m_mem.json()["error"]["code"] == "INVALID_TOKEN"

        # Admin caller with user JWT -> 401
        r_m_adm = await client.get("/metrics", headers={"Authorization": f"Bearer {admin_token}"})
        assert r_m_adm.status_code == 401
        assert r_m_adm.json()["error"]["code"] == "INVALID_TOKEN"

        # Disabled caller with user JWT -> 401
        r_m_dis = await client.get(
            "/metrics", headers={"Authorization": f"Bearer {disabled_token}"}
        )
        assert r_m_dis.status_code == 401
        assert r_m_dis.json()["error"]["code"] == "INVALID_TOKEN"

        # Caller with wrong metrics token -> 401
        r_m_wrong = await client.get(
            "/metrics", headers={"Authorization": "Bearer wrong-token-not-matching-at-all"}
        )
        assert r_m_wrong.status_code == 401
        assert r_m_wrong.json()["error"]["code"] == "INVALID_TOKEN"

        # Caller with valid metrics token -> 200 Prometheus text
        r_m_ok = await client.get(
            "/metrics", headers={"Authorization": f"Bearer {valid_metrics_token}"}
        )
        assert r_m_ok.status_code == 200
        assert "commonsbook_http_requests_total" in r_m_ok.text

        # 2. E02: POST /api/v1/auth/login (Policy.public) -> Accessible to all personas
        # Otherwise-valid request body:
        valid_login_body = {"email": member_user.email, "password": password}

        # Anonymous caller -> Allowed, receives 200
        r_anon = await client.post("/api/v1/auth/login", json=valid_login_body)
        assert r_anon.status_code == 200
        assert r_anon.json()["token_type"] == "bearer"

        # Member caller -> Allowed
        r_mem = await client.post(
            "/api/v1/auth/login",
            json=valid_login_body,
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_mem.status_code == 200

        # Admin caller -> Allowed
        r_adm = await client.post(
            "/api/v1/auth/login",
            json={"email": admin_user.email, "password": password},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_adm.status_code == 200

        # Disabled caller reaches endpoint: returns 401 INVALID_CREDENTIALS
        r_dis = await client.post(
            "/api/v1/auth/login",
            json={"email": disabled_user.email, "password": password},
            headers={"Authorization": f"Bearer {disabled_token}"},
        )
        assert r_dis.status_code == 401
        assert r_dis.json()["error"]["code"] == "INVALID_CREDENTIALS"

        # 3. E05: GET /api/v1/me (Policy.authenticated)
        # Member and Admin allowed; Anonymous and Disabled denied
        # Anonymous -> Denied with 401 AUTH_REQUIRED
        r_e05_anon = await client.get("/api/v1/me")
        assert r_e05_anon.status_code == 401
        assert r_e05_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        # Disabled user -> Denied with 401 INVALID_TOKEN
        r_e05_dis = await client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {disabled_token}"}
        )
        assert r_e05_dis.status_code == 401
        assert r_e05_dis.json()["error"]["code"] == "INVALID_TOKEN"

        # Member -> Allowed with 200 OK
        r_e05_mem = await client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {member_token}"}
        )
        assert r_e05_mem.status_code == 200
        assert r_e05_mem.json()["id"] == str(member_user.id)
        assert r_e05_mem.json()["role"] == "member"

        # Admin -> Allowed with 200 OK
        r_e05_adm = await client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert r_e05_adm.status_code == 200
        assert r_e05_adm.json()["id"] == str(admin_user.id)
        assert r_e05_adm.json()["role"] == "admin"

        # 4. E03: POST /api/v1/auth/refresh (Policy.public)
        # Accessible to all personas; disabled user gets 401 INVALID_REFRESH
        now = datetime.now(timezone.utc)
        mem_rt = await create_refresh_token(member_user, now)
        adm_rt = await create_refresh_token(admin_user, now)
        dis_rt = await create_refresh_token(disabled_user, now)
        anon_user = await create_user(role="member", enabled=True)
        anon_rt = await create_refresh_token(anon_user, now)

        # Anonymous caller with valid cookie and origin -> 200 TokenResponse
        client.cookies.set("commonsbook_rt", anon_rt)
        r_e03_anon = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert r_e03_anon.status_code == 200
        assert r_e03_anon.json()["token_type"] == "bearer"

        # Member caller with valid cookie and origin -> 200 TokenResponse
        client.cookies.set("commonsbook_rt", mem_rt)
        r_e03_mem = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173", "Authorization": f"Bearer {member_token}"},
        )
        assert r_e03_mem.status_code == 200
        assert r_e03_mem.json()["token_type"] == "bearer"

        # Admin caller with valid cookie and origin -> 200 TokenResponse
        client.cookies.set("commonsbook_rt", adm_rt)
        r_e03_adm = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173", "Authorization": f"Bearer {admin_token}"},
        )
        assert r_e03_adm.status_code == 200
        assert r_e03_adm.json()["token_type"] == "bearer"

        # Disabled caller reaches endpoint: returns 401 INVALID_REFRESH
        client.cookies.set("commonsbook_rt", dis_rt)
        r_e03_dis = await client.post(
            "/api/v1/auth/refresh",
            headers={
                "Origin": "http://localhost:5173",
                "Authorization": f"Bearer {disabled_token}",
            },
        )
        assert r_e03_dis.status_code == 401
        assert r_e03_dis.json()["error"]["code"] == "INVALID_REFRESH"

        # 5. E04: POST /api/v1/auth/logout (Policy.public)
        # Accessible to all personas; returns 204 No Content
        for token_val in [None, member_token, admin_token, disabled_token]:
            headers = {"Origin": "http://localhost:5173"}
            if token_val:
                headers["Authorization"] = f"Bearer {token_val}"
            r_e04 = await client.post("/api/v1/auth/logout", headers=headers)
            assert r_e04.status_code == 204

        # 6. E01: POST /api/v1/auth/register (Policy.public)
        # Accessible to all personas; invalid invitation returns 422 INVALID_INVITATION
        dummy_register_body = {
            "invitation_token": "non-existent-dummy-token",
            "email": f"perm_reg_{uuid.uuid4().hex[:8]}@example.com",
            "password": "valid-password-123",
            "display_name": "Registered User",
        }
        for token_val in [None, member_token, admin_token, disabled_token]:
            headers = {"Authorization": f"Bearer {token_val}"} if token_val else {}
            r_e01 = await client.post(
                "/api/v1/auth/register",
                json=dummy_register_body,
                headers=headers,
            )
            assert r_e01.status_code == 422
            assert r_e01.json()["error"]["code"] == "INVALID_INVITATION"

        # 7. E29: POST /api/v1/admin/invitations (Policy.admin)
        # Admin allowed (201); Member denied (403); Anonymous denied (401); Disabled denied (401)
        valid_invite_body = {
            "email": f"perm_inv_{uuid.uuid4().hex[:8]}@example.com",
            "role": "member",
        }
        r_e29_anon = await client.post("/api/v1/admin/invitations", json=valid_invite_body)
        assert r_e29_anon.status_code == 401
        assert r_e29_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        r_e29_dis = await client.post(
            "/api/v1/admin/invitations",
            json=valid_invite_body,
            headers={"Authorization": f"Bearer {disabled_token}"},
        )
        assert r_e29_dis.status_code == 401
        assert r_e29_dis.json()["error"]["code"] == "INVALID_TOKEN"

        r_e29_mem = await client.post(
            "/api/v1/admin/invitations",
            json=valid_invite_body,
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_e29_mem.status_code == 403
        assert r_e29_mem.json()["error"]["code"] == "FORBIDDEN"

        r_e29_adm = await client.post(
            "/api/v1/admin/invitations",
            json=valid_invite_body,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_e29_adm.status_code == 201
        assert r_e29_adm.json()["email"] == valid_invite_body["email"]
        assert r_e29_adm.json()["role"] == "member"
        assert "invitation_url" in r_e29_adm.json()
        assert r_e29_adm.headers.get("cache-control") == "no-store"

        # 8. E06: GET /api/v1/resources (Policy.authenticated)
        # Member and Admin allowed; Anonymous and Disabled denied
        r_e06_anon = await client.get("/api/v1/resources?limit=10&offset=0")
        assert r_e06_anon.status_code == 401
        assert r_e06_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        r_e06_dis = await client.get(
            "/api/v1/resources?limit=10&offset=0",
            headers={"Authorization": f"Bearer {disabled_token}"},
        )
        assert r_e06_dis.status_code == 401
        assert r_e06_dis.json()["error"]["code"] == "INVALID_TOKEN"

        r_e06_mem = await client.get(
            "/api/v1/resources?limit=10&offset=0",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_e06_mem.status_code == 200
        assert "items" in r_e06_mem.json()

        r_e06_adm = await client.get(
            "/api/v1/resources?limit=10&offset=0",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_e06_adm.status_code == 200
        assert "items" in r_e06_adm.json()

        # Seed real active resource for E07 and E08 matrix cells
        perm_res = Resource(
            id=uuid.uuid4(),
            name=f"PermResource_{uuid.uuid4().hex[:6]}",
            description="For permissions matrix",
            location="Room 101",
            active=True,
            version=1,
        )
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            async with session.begin():
                session.add(perm_res)

        # 9. E07: GET /api/v1/resources/{id} (Policy.authenticated)
        r_e07_anon = await client.get(f"/api/v1/resources/{perm_res.id}")
        assert r_e07_anon.status_code == 401
        assert r_e07_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        r_e07_dis = await client.get(
            f"/api/v1/resources/{perm_res.id}",
            headers={"Authorization": f"Bearer {disabled_token}"},
        )
        assert r_e07_dis.status_code == 401
        assert r_e07_dis.json()["error"]["code"] == "INVALID_TOKEN"

        r_e07_mem = await client.get(
            f"/api/v1/resources/{perm_res.id}",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_e07_mem.status_code == 200
        assert r_e07_mem.json()["id"] == str(perm_res.id)
        assert r_e07_mem.headers.get("etag") == '"1"'

        r_e07_adm = await client.get(
            f"/api/v1/resources/{perm_res.id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_e07_adm.status_code == 200
        assert r_e07_adm.json()["id"] == str(perm_res.id)
        assert r_e07_adm.headers.get("etag") == '"1"'

        # 10. E08: GET /api/v1/resources/{id}/availability (Policy.authenticated)
        avail_params = {
            "starts_at": "2026-08-01T10:00:00Z",
            "ends_at": "2026-08-01T12:00:00Z",
        }
        r_e08_anon = await client.get(
            f"/api/v1/resources/{perm_res.id}/availability",
            params=avail_params,
        )
        assert r_e08_anon.status_code == 401
        assert r_e08_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        r_e08_dis = await client.get(
            f"/api/v1/resources/{perm_res.id}/availability",
            params=avail_params,
            headers={"Authorization": f"Bearer {disabled_token}"},
        )
        assert r_e08_dis.status_code == 401
        assert r_e08_dis.json()["error"]["code"] == "INVALID_TOKEN"

        r_e08_mem = await client.get(
            f"/api/v1/resources/{perm_res.id}/availability",
            params=avail_params,
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_e08_mem.status_code == 200
        assert r_e08_mem.json()["resource_id"] == str(perm_res.id)

        r_e08_adm = await client.get(
            f"/api/v1/resources/{perm_res.id}/availability",
            params=avail_params,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_e08_adm.status_code == 200
        assert r_e08_adm.json()["resource_id"] == str(perm_res.id)

        # 11. E09: POST /api/v1/bookings (Policy.authenticated)
        # Member/Admin allowed (201); Anonymous denied (401 AUTH_REQUIRED);
        # Disabled denied (401 INVALID_TOKEN)
        # All requests provide otherwise-VALID body and distinct valid UUID v4 Idempotency-Key
        e09_res = Resource(
            id=uuid.uuid4(),
            name=f"E09Resource_{uuid.uuid4().hex[:6]}",
            description="For E09 permissions matrix",
            location="Room 102",
            active=True,
            version=1,
        )
        async with sessionmaker() as session:
            async with session.begin():
                session.add(e09_res)

        slot_base = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        ) + timedelta(days=2)

        # Anon request
        anon_slot_start = (slot_base + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        anon_slot_end = (slot_base + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r_e09_anon = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": str(e09_res.id),
                "starts_at": anon_slot_start,
                "ends_at": anon_slot_end,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert r_e09_anon.status_code == 401
        assert r_e09_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        # Disabled request
        dis_slot_start = (slot_base + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        dis_slot_end = (slot_base + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r_e09_dis = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": str(e09_res.id),
                "starts_at": dis_slot_start,
                "ends_at": dis_slot_end,
            },
            headers={
                "Authorization": f"Bearer {disabled_token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        assert r_e09_dis.status_code == 401
        assert r_e09_dis.json()["error"]["code"] == "INVALID_TOKEN"

        # Member request -> 201
        mem_slot_start = (slot_base + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        mem_slot_end = (slot_base + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r_e09_mem = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": str(e09_res.id),
                "starts_at": mem_slot_start,
                "ends_at": mem_slot_end,
            },
            headers={
                "Authorization": f"Bearer {member_token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        assert r_e09_mem.status_code == 201
        assert r_e09_mem.json()["user_id"] == str(member_user.id)
        assert r_e09_mem.json()["resource_id"] == str(e09_res.id)
        assert r_e09_mem.json()["status"] == "confirmed"

        # Admin request -> 201
        adm_slot_start = (slot_base + timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        adm_slot_end = (slot_base + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r_e09_adm = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": str(e09_res.id),
                "starts_at": adm_slot_start,
                "ends_at": adm_slot_end,
            },
            headers={
                "Authorization": f"Bearer {admin_token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        assert r_e09_adm.status_code == 201
        assert r_e09_adm.json()["user_id"] == str(admin_user.id)
        assert r_e09_adm.json()["resource_id"] == str(e09_res.id)
        assert r_e09_adm.json()["status"] == "confirmed"

        member_booking_id = r_e09_mem.json()["id"]
        admin_booking_id = r_e09_adm.json()["id"]

        # 12. E10: GET /api/v1/bookings (Policy.own_booking)
        # Member and Admin each see only their own reservations;
        # Anonymous denied (401 AUTH_REQUIRED); Disabled denied (401 INVALID_TOKEN)
        r_e10_anon = await client.get("/api/v1/bookings?limit=100&offset=0")
        assert r_e10_anon.status_code == 401
        assert r_e10_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        r_e10_dis = await client.get(
            "/api/v1/bookings?limit=100&offset=0",
            headers={"Authorization": f"Bearer {disabled_token}"},
        )
        assert r_e10_dis.status_code == 401
        assert r_e10_dis.json()["error"]["code"] == "INVALID_TOKEN"

        r_e10_mem = await client.get(
            "/api/v1/bookings?limit=100&offset=0",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_e10_mem.status_code == 200
        mem_listed = [item["id"] for item in r_e10_mem.json()["items"]]
        assert member_booking_id in mem_listed
        assert admin_booking_id not in mem_listed

        r_e10_adm = await client.get(
            "/api/v1/bookings?limit=100&offset=0",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_e10_adm.status_code == 200
        adm_listed = [item["id"] for item in r_e10_adm.json()["items"]]
        assert admin_booking_id in adm_listed
        assert member_booking_id not in adm_listed

        # 13. E11: GET /api/v1/bookings/{id} (Policy.own_booking)
        # Owner allowed; a foreign booking is 404 for members and admins alike
        r_e11_anon = await client.get(f"/api/v1/bookings/{member_booking_id}")
        assert r_e11_anon.status_code == 401
        assert r_e11_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        r_e11_dis = await client.get(
            f"/api/v1/bookings/{member_booking_id}",
            headers={"Authorization": f"Bearer {disabled_token}"},
        )
        assert r_e11_dis.status_code == 401
        assert r_e11_dis.json()["error"]["code"] == "INVALID_TOKEN"

        r_e11_mem_own = await client.get(
            f"/api/v1/bookings/{member_booking_id}",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_e11_mem_own.status_code == 200
        assert r_e11_mem_own.json()["id"] == member_booking_id
        assert r_e11_mem_own.headers.get("etag") == '"1"'

        r_e11_mem_other = await client.get(
            f"/api/v1/bookings/{admin_booking_id}",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_e11_mem_other.status_code == 404
        assert r_e11_mem_other.json()["error"]["code"] == "NOT_FOUND"

        r_e11_adm_own = await client.get(
            f"/api/v1/bookings/{admin_booking_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_e11_adm_own.status_code == 200
        assert r_e11_adm_own.json()["id"] == admin_booking_id

        r_e11_adm_other = await client.get(
            f"/api/v1/bookings/{member_booking_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_e11_adm_other.status_code == 404
        assert r_e11_adm_other.json()["error"]["code"] == "NOT_FOUND"

        # 14. E12: POST /api/v1/bookings/{id}/cancel (Policy.own_booking)
        # Every request below carries an otherwise-valid body and a current If-Match,
        # so a denial cannot be mistaken for a precondition or validation failure.
        cancel_body = {"reason": "permission matrix"}
        current_if_match = {"If-Match": '"1"'}

        r_e12_anon = await client.post(
            f"/api/v1/bookings/{member_booking_id}/cancel",
            json=cancel_body,
            headers=current_if_match,
        )
        assert r_e12_anon.status_code == 401
        assert r_e12_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        r_e12_dis = await client.post(
            f"/api/v1/bookings/{member_booking_id}/cancel",
            json=cancel_body,
            headers={**current_if_match, "Authorization": f"Bearer {disabled_token}"},
        )
        assert r_e12_dis.status_code == 401
        assert r_e12_dis.json()["error"]["code"] == "INVALID_TOKEN"

        r_e12_mem_other = await client.post(
            f"/api/v1/bookings/{admin_booking_id}/cancel",
            json=cancel_body,
            headers={**current_if_match, "Authorization": f"Bearer {member_token}"},
        )
        assert r_e12_mem_other.status_code == 404
        assert r_e12_mem_other.json()["error"]["code"] == "NOT_FOUND"

        r_e12_adm_other = await client.post(
            f"/api/v1/bookings/{member_booking_id}/cancel",
            json=cancel_body,
            headers={**current_if_match, "Authorization": f"Bearer {admin_token}"},
        )
        assert r_e12_adm_other.status_code == 404
        assert r_e12_adm_other.json()["error"]["code"] == "NOT_FOUND"

        r_e12_mem_own = await client.post(
            f"/api/v1/bookings/{member_booking_id}/cancel",
            json=cancel_body,
            headers={**current_if_match, "Authorization": f"Bearer {member_token}"},
        )
        assert r_e12_mem_own.status_code == 200
        assert r_e12_mem_own.json()["status"] == "cancelled"
        assert r_e12_mem_own.json()["version"] == 2
        assert r_e12_mem_own.headers.get("etag") == '"2"'

        r_e12_adm_own = await client.post(
            f"/api/v1/bookings/{admin_booking_id}/cancel",
            json=cancel_body,
            headers={**current_if_match, "Authorization": f"Bearer {admin_token}"},
        )
        assert r_e12_adm_own.status_code == 200
        assert r_e12_adm_own.json()["status"] == "cancelled"
        assert r_e12_adm_own.json()["user_id"] == str(admin_user.id)

        # 15. E13: POST /api/v1/waitlist (Policy.authenticated)
        # Member/Admin allowed (201); Anonymous denied (401 AUTH_REQUIRED);
        # Disabled denied (401 INVALID_TOKEN).
        e13_res = Resource(
            id=uuid.uuid4(),
            name=f"E13Resource_{uuid.uuid4().hex[:6]}",
            description="For E13 permissions matrix",
            location="Room 104",
            active=True,
            version=1,
        )
        dummy_creator = await create_user()
        e13_slot_start = (slot_base + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        e13_slot_end = (slot_base + timedelta(hours=11)).strftime("%Y-%m-%dT%H:%M:%SZ")
        active_b = Booking(
            id=uuid.uuid4(),
            resource_id=e13_res.id,
            user_id=dummy_creator.id,
            created_by=dummy_creator.id,
            kind="reservation",
            time_range=Range(
                slot_base + timedelta(hours=10),
                slot_base + timedelta(hours=11),
                bounds="[)",
            ),
            status="confirmed",
            version=1,
        )
        async with sessionmaker() as session:
            async with session.begin():
                session.add(e13_res)
                await session.flush()
                session.add(active_b)

        wait_create_body = {
            "resource_id": str(e13_res.id),
            "starts_at": e13_slot_start,
            "ends_at": e13_slot_end,
        }

        # Anon -> 401
        r_e13_anon = await client.post("/api/v1/waitlist", json=wait_create_body)
        assert r_e13_anon.status_code == 401
        assert r_e13_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        # Disabled -> 401
        r_e13_dis = await client.post(
            "/api/v1/waitlist",
            json=wait_create_body,
            headers={"Authorization": f"Bearer {disabled_token}"},
        )
        assert r_e13_dis.status_code == 401
        assert r_e13_dis.json()["error"]["code"] == "INVALID_TOKEN"

        # Member -> 201
        r_e13_mem = await client.post(
            "/api/v1/waitlist",
            json=wait_create_body,
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_e13_mem.status_code == 201
        assert r_e13_mem.json()["user_id"] == str(member_user.id)
        member_waitlist_id = r_e13_mem.json()["id"]

        # Admin -> 201
        r_e13_adm = await client.post(
            "/api/v1/waitlist",
            json=wait_create_body,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_e13_adm.status_code == 201
        assert r_e13_adm.json()["user_id"] == str(admin_user.id)
        admin_waitlist_id = r_e13_adm.json()["id"]

        # 16. E14: GET /api/v1/waitlist (Policy.own_waitlist)
        # Member and Admin each see only their own entries;
        # Anonymous denied (401 AUTH_REQUIRED); Disabled denied (401 INVALID_TOKEN)
        r_e14_anon = await client.get("/api/v1/waitlist?limit=100&offset=0")
        assert r_e14_anon.status_code == 401
        assert r_e14_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        r_e14_dis = await client.get(
            "/api/v1/waitlist?limit=100&offset=0",
            headers={"Authorization": f"Bearer {disabled_token}"},
        )
        assert r_e14_dis.status_code == 401
        assert r_e14_dis.json()["error"]["code"] == "INVALID_TOKEN"

        r_e14_mem = await client.get(
            "/api/v1/waitlist?limit=100&offset=0",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_e14_mem.status_code == 200
        mem_wl_items = [x["id"] for x in r_e14_mem.json()["items"]]
        assert member_waitlist_id in mem_wl_items
        assert admin_waitlist_id not in mem_wl_items

        r_e14_adm = await client.get(
            "/api/v1/waitlist?limit=100&offset=0",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_e14_adm.status_code == 200
        adm_wl_items = [x["id"] for x in r_e14_adm.json()["items"]]
        assert admin_waitlist_id in adm_wl_items
        assert member_waitlist_id not in adm_wl_items

        # 17. E15: DELETE /api/v1/waitlist/{id} (Policy.own_waitlist)
        # Foreign entry returns 404 for member and admin alike; owner succeeds with 200.
        r_e15_anon = await client.delete(
            f"/api/v1/waitlist/{member_waitlist_id}",
            headers=current_if_match,
        )
        assert r_e15_anon.status_code == 401
        assert r_e15_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        r_e15_dis = await client.delete(
            f"/api/v1/waitlist/{member_waitlist_id}",
            headers={**current_if_match, "Authorization": f"Bearer {disabled_token}"},
        )
        assert r_e15_dis.status_code == 401
        assert r_e15_dis.json()["error"]["code"] == "INVALID_TOKEN"

        r_e15_mem_other = await client.delete(
            f"/api/v1/waitlist/{admin_waitlist_id}",
            headers={**current_if_match, "Authorization": f"Bearer {member_token}"},
        )
        assert r_e15_mem_other.status_code == 404
        assert r_e15_mem_other.json()["error"]["code"] == "NOT_FOUND"

        r_e15_adm_other = await client.delete(
            f"/api/v1/waitlist/{member_waitlist_id}",
            headers={**current_if_match, "Authorization": f"Bearer {admin_token}"},
        )
        assert r_e15_adm_other.status_code == 404
        assert r_e15_adm_other.json()["error"]["code"] == "NOT_FOUND"

        r_e15_mem_own = await client.delete(
            f"/api/v1/waitlist/{member_waitlist_id}",
            headers={**current_if_match, "Authorization": f"Bearer {member_token}"},
        )
        assert r_e15_mem_own.status_code == 200
        assert r_e15_mem_own.json()["status"] == "cancelled"
        assert r_e15_mem_own.json()["version"] == 2

        r_e15_adm_own = await client.delete(
            f"/api/v1/waitlist/{admin_waitlist_id}",
            headers={**current_if_match, "Authorization": f"Bearer {admin_token}"},
        )
        assert r_e15_adm_own.status_code == 200
        assert r_e15_adm_own.json()["status"] == "cancelled"

        # 18. E16: POST /api/v1/waitlist/{id}/accept (Policy.own_waitlist)
        # Create offered entries for member and admin
        now = datetime.now(timezone.utc)
        mem_offered_b = Booking(
            id=uuid.uuid4(),
            resource_id=e13_res.id,
            user_id=member_user.id,
            created_by=member_user.id,
            kind="reservation",
            time_range=Range(
                slot_base + timedelta(hours=14),
                slot_base + timedelta(hours=15),
                bounds="[)",
            ),
            status="offered",
            expires_at=now + timedelta(minutes=15),
            version=1,
        )
        mem_offered_entry = WaitlistEntry(
            id=uuid.uuid4(),
            user_id=member_user.id,
            resource_id=e13_res.id,
            time_range=Range(
                slot_base + timedelta(hours=14),
                slot_base + timedelta(hours=15),
                bounds="[)",
            ),
            status="offered",
            offered_booking_id=mem_offered_b.id,
            version=1,
        )

        adm_offered_b = Booking(
            id=uuid.uuid4(),
            resource_id=e13_res.id,
            user_id=admin_user.id,
            created_by=admin_user.id,
            kind="reservation",
            time_range=Range(
                slot_base + timedelta(hours=16),
                slot_base + timedelta(hours=17),
                bounds="[)",
            ),
            status="offered",
            expires_at=now + timedelta(minutes=15),
            version=1,
        )
        adm_offered_entry = WaitlistEntry(
            id=uuid.uuid4(),
            user_id=admin_user.id,
            resource_id=e13_res.id,
            time_range=Range(
                slot_base + timedelta(hours=16),
                slot_base + timedelta(hours=17),
                bounds="[)",
            ),
            status="offered",
            offered_booking_id=adm_offered_b.id,
            version=1,
        )

        async with sessionmaker() as session:
            async with session.begin():
                session.add(mem_offered_b)
                session.add(adm_offered_b)
                await session.flush()
                session.add(mem_offered_entry)
                session.add(adm_offered_entry)

        # Anon -> 401
        r_e16_anon = await client.post(
            f"/api/v1/waitlist/{mem_offered_entry.id}/accept",
            json={},
            headers=current_if_match,
        )
        assert r_e16_anon.status_code == 401
        assert r_e16_anon.json()["error"]["code"] == "AUTH_REQUIRED"

        # Disabled -> 401
        r_e16_dis = await client.post(
            f"/api/v1/waitlist/{mem_offered_entry.id}/accept",
            json={},
            headers={**current_if_match, "Authorization": f"Bearer {disabled_token}"},
        )
        assert r_e16_dis.status_code == 401
        assert r_e16_dis.json()["error"]["code"] == "INVALID_TOKEN"

        # Member other -> 404
        r_e16_mem_other = await client.post(
            f"/api/v1/waitlist/{adm_offered_entry.id}/accept",
            json={},
            headers={**current_if_match, "Authorization": f"Bearer {member_token}"},
        )
        assert r_e16_mem_other.status_code == 404
        assert r_e16_mem_other.json()["error"]["code"] == "NOT_FOUND"

        # Admin other -> 404
        r_e16_adm_other = await client.post(
            f"/api/v1/waitlist/{mem_offered_entry.id}/accept",
            json={},
            headers={**current_if_match, "Authorization": f"Bearer {admin_token}"},
        )
        assert r_e16_adm_other.status_code == 404
        assert r_e16_adm_other.json()["error"]["code"] == "NOT_FOUND"

        # Member own -> 200
        r_e16_mem_own = await client.post(
            f"/api/v1/waitlist/{mem_offered_entry.id}/accept",
            json={},
            headers={**current_if_match, "Authorization": f"Bearer {member_token}"},
        )
        assert r_e16_mem_own.status_code == 200
        assert r_e16_mem_own.json()["status"] == "confirmed"

        # Admin own -> 200
        r_e16_adm_own = await client.post(
            f"/api/v1/waitlist/{adm_offered_entry.id}/accept",
            json={},
            headers={**current_if_match, "Authorization": f"Bearer {admin_token}"},
        )
        assert r_e16_adm_own.status_code == 200
        assert r_e16_adm_own.json()["status"] == "confirmed"

        # Seed fixtures for E17–E26 admin permission matrix
        admin_res = Resource(
            id=uuid.uuid4(),
            name=f"AdminPermRes_{uuid.uuid4().hex[:6]}",
            description="Resource for admin permissions matrix",
            location="Room 105",
            active=True,
            version=1,
        )
        admin_res_arch = Resource(
            id=uuid.uuid4(),
            name=f"AdminPermArch_{uuid.uuid4().hex[:6]}",
            description="Resource to archive for admin permissions matrix",
            location="Room 106",
            active=True,
            version=1,
        )
        admin_bkg = Booking(
            id=uuid.uuid4(),
            resource_id=admin_res.id,
            user_id=member_user.id,
            created_by=member_user.id,
            kind="reservation",
            time_range=Range(
                slot_base + timedelta(hours=20),
                slot_base + timedelta(hours=21),
                bounds="[)",
            ),
            status="confirmed",
            version=1,
        )
        admin_blackout = Booking(
            id=uuid.uuid4(),
            resource_id=admin_res.id,
            user_id=None,
            created_by=admin_user.id,
            kind="blackout",
            time_range=Range(
                slot_base + timedelta(hours=22),
                slot_base + timedelta(hours=23),
                bounds="[)",
            ),
            status="confirmed",
            version=1,
        )
        async with sessionmaker() as session:
            async with session.begin():
                session.add(admin_res)
                session.add(admin_res_arch)
                await session.flush()
                session.add(admin_bkg)
                session.add(admin_blackout)

        h_dis = {"Authorization": f"Bearer {disabled_token}"}
        h_mem = {"Authorization": f"Bearer {member_token}"}
        h_adm = {"Authorization": f"Bearer {admin_token}"}
        m_if1 = {"If-Match": '"1"'}

        # 19. E17: GET /api/v1/admin/resources (Policy.admin)
        assert (await client.get("/api/v1/admin/resources")).status_code == 401
        assert (await client.get("/api/v1/admin/resources", headers=h_dis)).status_code == 401
        assert (await client.get("/api/v1/admin/resources", headers=h_mem)).status_code == 403
        r_e17_adm = await client.get("/api/v1/admin/resources", headers=h_adm)
        assert r_e17_adm.status_code == 200

        # 20. E18: POST /api/v1/admin/resources (Policy.admin)
        valid_res_create = {"name": f"NewRes_{uuid.uuid4().hex[:6]}", "location": "Room 107"}
        assert (
            await client.post("/api/v1/admin/resources", json=valid_res_create)
        ).status_code == 401
        assert (
            await client.post("/api/v1/admin/resources", json=valid_res_create, headers=h_dis)
        ).status_code == 401
        assert (
            await client.post("/api/v1/admin/resources", json=valid_res_create, headers=h_mem)
        ).status_code == 403
        r_e18_adm = await client.post(
            "/api/v1/admin/resources", json=valid_res_create, headers=h_adm
        )
        assert r_e18_adm.status_code == 201

        # 21. E19: PATCH /api/v1/admin/resources/{id} (Policy.admin)
        valid_res_patch = {"name": "PatchedName"}
        assert (
            await client.patch(
                f"/api/v1/admin/resources/{admin_res.id}", json=valid_res_patch, headers=m_if1
            )
        ).status_code == 401
        assert (
            await client.patch(
                f"/api/v1/admin/resources/{admin_res.id}",
                json=valid_res_patch,
                headers={**m_if1, **h_dis},
            )
        ).status_code == 401
        assert (
            await client.patch(
                f"/api/v1/admin/resources/{admin_res.id}",
                json=valid_res_patch,
                headers={**m_if1, **h_mem},
            )
        ).status_code == 403
        r_e19_adm = await client.patch(
            f"/api/v1/admin/resources/{admin_res.id}",
            json=valid_res_patch,
            headers={**m_if1, **h_adm},
        )
        assert r_e19_adm.status_code == 200

        # 22. E20: DELETE /api/v1/admin/resources/{id} (Policy.admin)
        assert (
            await client.delete(f"/api/v1/admin/resources/{admin_res_arch.id}", headers=m_if1)
        ).status_code == 401
        assert (
            await client.delete(
                f"/api/v1/admin/resources/{admin_res_arch.id}", headers={**m_if1, **h_dis}
            )
        ).status_code == 401
        assert (
            await client.delete(
                f"/api/v1/admin/resources/{admin_res_arch.id}", headers={**m_if1, **h_mem}
            )
        ).status_code == 403
        r_e20_adm = await client.delete(
            f"/api/v1/admin/resources/{admin_res_arch.id}", headers={**m_if1, **h_adm}
        )
        assert r_e20_adm.status_code == 200

        # 23. E21: GET /api/v1/admin/resources/{id}/blackouts (Policy.admin)
        assert (
            await client.get(f"/api/v1/admin/resources/{admin_res.id}/blackouts")
        ).status_code == 401
        assert (
            await client.get(f"/api/v1/admin/resources/{admin_res.id}/blackouts", headers=h_dis)
        ).status_code == 401
        assert (
            await client.get(f"/api/v1/admin/resources/{admin_res.id}/blackouts", headers=h_mem)
        ).status_code == 403
        r_e21_adm = await client.get(
            f"/api/v1/admin/resources/{admin_res.id}/blackouts", headers=h_adm
        )
        assert r_e21_adm.status_code == 200

        # 24. E22: POST /api/v1/admin/resources/{id}/blackouts (Policy.admin)
        valid_bo_create = {
            "starts_at": (slot_base + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ends_at": (slot_base + timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        h_id_anon = {"Idempotency-Key": str(uuid.uuid4())}
        h_id_dis = {"Idempotency-Key": str(uuid.uuid4()), **h_dis}
        h_id_mem = {"Idempotency-Key": str(uuid.uuid4()), **h_mem}
        h_id_adm = {"Idempotency-Key": str(uuid.uuid4()), **h_adm}

        assert (
            await client.post(
                f"/api/v1/admin/resources/{admin_res.id}/blackouts",
                json=valid_bo_create,
                headers=h_id_anon,
            )
        ).status_code == 401
        assert (
            await client.post(
                f"/api/v1/admin/resources/{admin_res.id}/blackouts",
                json=valid_bo_create,
                headers=h_id_dis,
            )
        ).status_code == 401
        assert (
            await client.post(
                f"/api/v1/admin/resources/{admin_res.id}/blackouts",
                json=valid_bo_create,
                headers=h_id_mem,
            )
        ).status_code == 403
        r_e22_adm = await client.post(
            f"/api/v1/admin/resources/{admin_res.id}/blackouts",
            json=valid_bo_create,
            headers=h_id_adm,
        )
        assert r_e22_adm.status_code == 201

        # 25. E23: DELETE /api/v1/admin/blackouts/{id} (Policy.admin)
        assert (
            await client.delete(f"/api/v1/admin/blackouts/{admin_blackout.id}", headers=m_if1)
        ).status_code == 401
        assert (
            await client.delete(
                f"/api/v1/admin/blackouts/{admin_blackout.id}", headers={**m_if1, **h_dis}
            )
        ).status_code == 401
        assert (
            await client.delete(
                f"/api/v1/admin/blackouts/{admin_blackout.id}", headers={**m_if1, **h_mem}
            )
        ).status_code == 403
        r_e23_adm = await client.delete(
            f"/api/v1/admin/blackouts/{admin_blackout.id}", headers={**m_if1, **h_adm}
        )
        assert r_e23_adm.status_code == 200

        # 26. E24: GET /api/v1/admin/bookings (Policy.admin)
        assert (await client.get("/api/v1/admin/bookings")).status_code == 401
        assert (await client.get("/api/v1/admin/bookings", headers=h_dis)).status_code == 401
        assert (await client.get("/api/v1/admin/bookings", headers=h_mem)).status_code == 403
        r_e24_adm = await client.get("/api/v1/admin/bookings", headers=h_adm)
        assert r_e24_adm.status_code == 200

        # 27. E25: GET /api/v1/admin/bookings/{id} (Policy.admin)
        assert (await client.get(f"/api/v1/admin/bookings/{admin_bkg.id}")).status_code == 401
        assert (
            await client.get(f"/api/v1/admin/bookings/{admin_bkg.id}", headers=h_dis)
        ).status_code == 401
        assert (
            await client.get(f"/api/v1/admin/bookings/{admin_bkg.id}", headers=h_mem)
        ).status_code == 403
        r_e25_adm = await client.get(f"/api/v1/admin/bookings/{admin_bkg.id}", headers=h_adm)
        assert r_e25_adm.status_code == 200

        # 28. E26: POST /api/v1/admin/bookings/{id}/cancel (Policy.admin)
        cancel_b = {"reason": "matrix cancel"}
        assert (
            await client.post(
                f"/api/v1/admin/bookings/{admin_bkg.id}/cancel", json=cancel_b, headers=m_if1
            )
        ).status_code == 401
        assert (
            await client.post(
                f"/api/v1/admin/bookings/{admin_bkg.id}/cancel",
                json=cancel_b,
                headers={**m_if1, **h_dis},
            )
        ).status_code == 401
        assert (
            await client.post(
                f"/api/v1/admin/bookings/{admin_bkg.id}/cancel",
                json=cancel_b,
                headers={**m_if1, **h_mem},
            )
        ).status_code == 403
        r_e26_adm = await client.post(
            f"/api/v1/admin/bookings/{admin_bkg.id}/cancel",
            json=cancel_b,
            headers={**m_if1, **h_adm},
        )
        assert r_e26_adm.status_code == 200

        # 29. E27: GET /api/v1/admin/users (Policy.admin)
        assert (await client.get("/api/v1/admin/users")).status_code == 401
        assert (await client.get("/api/v1/admin/users", headers=h_dis)).status_code == 401
        assert (await client.get("/api/v1/admin/users", headers=h_mem)).status_code == 403
        r_e27_adm = await client.get("/api/v1/admin/users", headers=h_adm)
        assert r_e27_adm.status_code == 200

        # 30. E28: PATCH /api/v1/admin/users/{id} (Policy.admin)
        target_perm_user = await create_user(role="member", enabled=True)
        patch_user_body = {"role": "member"}
        u_if = {"If-Match": f'"{target_perm_user.version}"'}
        assert (
            await client.patch(
                f"/api/v1/admin/users/{target_perm_user.id}",
                json=patch_user_body,
                headers=u_if,
            )
        ).status_code == 401
        assert (
            await client.patch(
                f"/api/v1/admin/users/{target_perm_user.id}",
                json=patch_user_body,
                headers={**u_if, **h_dis},
            )
        ).status_code == 401
        assert (
            await client.patch(
                f"/api/v1/admin/users/{target_perm_user.id}",
                json=patch_user_body,
                headers={**u_if, **h_mem},
            )
        ).status_code == 403
        r_e28_adm = await client.patch(
            f"/api/v1/admin/users/{target_perm_user.id}",
            json=patch_user_body,
            headers={**u_if, **h_adm},
        )
        assert r_e28_adm.status_code == 200

        # 31. E30: GET /api/v1/admin/audit (Policy.admin)
        assert (await client.get("/api/v1/admin/audit")).status_code == 401
        assert (await client.get("/api/v1/admin/audit", headers=h_dis)).status_code == 401
        assert (await client.get("/api/v1/admin/audit", headers=h_mem)).status_code == 403
        r_e30_adm = await client.get("/api/v1/admin/audit", headers=h_adm)
        assert r_e30_adm.status_code == 200

        # 32. E31: GET /api/v1/admin/outbox (Policy.admin)
        assert (await client.get("/api/v1/admin/outbox")).status_code == 401
        assert (await client.get("/api/v1/admin/outbox", headers=h_dis)).status_code == 401
        assert (await client.get("/api/v1/admin/outbox", headers=h_mem)).status_code == 403
        r_e31_adm = await client.get("/api/v1/admin/outbox", headers=h_adm)
        assert r_e31_adm.status_code == 200

        # 33. E32: POST /api/v1/admin/outbox/{id}/retry (Policy.admin)
        dead_outbox_id = uuid.uuid4()
        async with sessionmaker() as session:
            async with session.begin():
                dead_row = Outbox(
                    id=dead_outbox_id,
                    event_type="booking_confirmed",
                    aggregate_id=admin_bkg.id,
                    aggregate_version=99,
                    payload={"schema_version": 1},
                    status="dead",
                    attempts=8,
                    occurred_at=datetime.now(timezone.utc),
                    available_at=datetime.now(timezone.utc),
                    last_error="fatal",
                )
                session.add(dead_row)

        assert (
            await client.post(f"/api/v1/admin/outbox/{dead_outbox_id}/retry", json={})
        ).status_code == 401
        assert (
            await client.post(
                f"/api/v1/admin/outbox/{dead_outbox_id}/retry", json={}, headers=h_dis
            )
        ).status_code == 401
        assert (
            await client.post(
                f"/api/v1/admin/outbox/{dead_outbox_id}/retry", json={}, headers=h_mem
            )
        ).status_code == 403
        r_e32_adm = await client.post(
            f"/api/v1/admin/outbox/{dead_outbox_id}/retry", json={}, headers=h_adm
        )
        assert r_e32_adm.status_code == 200

        # 34. E33: POST /api/v1/feedback (Policy.authenticated)
        fb_body = {
            "rating": 5,
            "task_completed": True,
            "difficulty": "none",
            "improvement": "none",
            "consent_version": "2026-09-v1",
            "consent": True,
        }
        assert (await client.post("/api/v1/feedback", json=fb_body)).status_code == 401
        assert (
            await client.post("/api/v1/feedback", json=fb_body, headers=h_dis)
        ).status_code == 401
        r_e33_mem = await client.post("/api/v1/feedback", json=fb_body, headers=h_mem)
        assert r_e33_mem.status_code == 201
        r_e33_adm = await client.post("/api/v1/feedback", json=fb_body, headers=h_adm)
        assert r_e33_adm.status_code == 201

        # 35. E34: GET /api/v1/admin/feedback (Policy.admin)
        assert (await client.get("/api/v1/admin/feedback")).status_code == 401
        assert (await client.get("/api/v1/admin/feedback", headers=h_dis)).status_code == 401
        assert (await client.get("/api/v1/admin/feedback", headers=h_mem)).status_code == 403
        r_e34_adm = await client.get("/api/v1/admin/feedback", headers=h_adm)
        assert r_e34_adm.status_code == 200


@pytest.mark.asyncio
async def test_own_booking_routes_reject_overposting_and_spoofed_claims() -> None:
    """Body fields and token claims cannot widen own-booking authorization."""
    member_user = await create_user(role="member", enabled=True)
    other_user = await create_user(role="member", enabled=True)
    member_token = make_token(member_user)

    resource = Resource(
        id=uuid.uuid4(),
        name=f"OverpostResource_{uuid.uuid4().hex[:6]}",
        description="For overposting cases",
        location="Room 103",
        active=True,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(resource)

    slot_base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(
        days=3
    )

    client_ip = f"10.8.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        created = await client.post(
            "/api/v1/bookings",
            json={
                "resource_id": str(resource.id),
                "starts_at": slot_base.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": (slot_base + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            headers={
                "Authorization": f"Bearer {member_token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        assert created.status_code == 201
        booking_id = created.json()["id"]

        # Extra body fields on the cancel request are rejected, never applied.
        for overposted in (
            {"reason": "x", "user_id": str(other_user.id)},
            {"reason": "x", "status": "confirmed"},
            {"reason": "x", "version": 99},
            {"reason": "x", "resource_id": str(uuid.uuid4())},
        ):
            r_overpost = await client.post(
                f"/api/v1/bookings/{booking_id}/cancel",
                json=overposted,
                headers={
                    "Authorization": f"Bearer {member_token}",
                    "If-Match": '"1"',
                },
            )
            assert r_overpost.status_code == 422, overposted
            assert r_overpost.json()["error"]["code"] == "VALIDATION_ERROR"

        # A spoofed admin role claim grants no access to a booking owned by someone else.
        settings = get_settings()
        jwt_secret = settings.JWT_SECRET or "test-jwt-secret-minimum-32-bytes-long-12345678"
        now = datetime.now(timezone.utc)
        spoofed_token = jwt.encode(
            {
                "sub": str(other_user.id),
                "role": "admin",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=15)).timestamp()),
                "jti": str(uuid.uuid4()),
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
            },
            jwt_secret,
            algorithm="HS256",
        )

        r_spoof_read = await client.get(
            f"/api/v1/bookings/{booking_id}",
            headers={"Authorization": f"Bearer {spoofed_token}"},
        )
        assert r_spoof_read.status_code == 404
        assert r_spoof_read.json()["error"]["code"] == "NOT_FOUND"

        r_spoof_cancel = await client.post(
            f"/api/v1/bookings/{booking_id}/cancel",
            json={"reason": "spoofed"},
            headers={"Authorization": f"Bearer {spoofed_token}", "If-Match": '"1"'},
        )
        assert r_spoof_cancel.status_code == 404
        assert r_spoof_cancel.json()["error"]["code"] == "NOT_FOUND"

        # The spoofed claim also grants no admin-only access.
        r_spoof_admin = await client.post(
            "/api/v1/admin/invitations",
            json={"email": f"perm_spoof_{uuid.uuid4().hex[:8]}@example.com", "role": "member"},
            headers={"Authorization": f"Bearer {spoofed_token}"},
        )
        assert r_spoof_admin.status_code == 403
        assert r_spoof_admin.json()["error"]["code"] == "FORBIDDEN"

    # The booking is untouched by every rejected attempt above.
    async with sessionmaker() as session:
        stored = (
            await session.execute(select(Booking).where(Booking.id == uuid.UUID(booking_id)))
        ).scalar_one()
        assert stored.status == "confirmed"
        assert stored.version == 1
        assert stored.user_id == member_user.id
