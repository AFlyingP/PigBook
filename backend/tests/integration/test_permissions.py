import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from alembic.config import Config

from alembic import command

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.auth.dependencies import Policy, policy_registry
from app.auth.models import User
from app.auth.passwords import hash_password
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app

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
    """Every registered route has policy metadata in policy_registry;

    only E02, E05, E35 are registered in this ticket.
    """
    assert set(policy_registry.keys()) == {"E02", "E05", "E35"}
    assert policy_registry["E02"] == Policy.public
    assert policy_registry["E05"] == Policy.authenticated
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
