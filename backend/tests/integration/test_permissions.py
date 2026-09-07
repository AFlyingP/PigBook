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

from app.auth.dependencies import Policy, policy_registry
from app.auth.models import RefreshToken, User
from app.auth.passwords import hash_password
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app
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
        "E29",
        "E35",
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
    assert policy_registry["E29"] == Policy.admin
    assert policy_registry["E35"] == Policy.public


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
