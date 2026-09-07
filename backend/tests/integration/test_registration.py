import asyncio
import base64
import hashlib
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
import pytest
from alembic.config import Config
from sqlalchemy import func, select

from alembic import command

# Ensure test secrets are set before importing app components
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.admin.models import AuditLog
from app.auth.models import Invitation, RefreshToken, User
from app.auth.passwords import hash_password, verify_password
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app

get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _ensure_schema() -> None:
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


async def create_test_user(
    *,
    email: str | None = None,
    password: str = "valid-password-123",
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
        display_name="Test User",
        role=role,
        enabled=enabled,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(user)
    return user


def make_admin_token(admin_user: User) -> str:
    settings = get_settings()
    jwt_secret = settings.JWT_SECRET or "test-jwt-secret-minimum-32-bytes-long-12345678"
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(admin_user.id),
        "role": "admin",
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


async def create_invitation_in_db(
    *,
    email: str,
    role: str = "member",
    creator_id: uuid.UUID,
    now: datetime,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    consumed: bool = False,
) -> tuple[str, Invitation]:
    raw_bytes = os.urandom(32)
    raw_token = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    c_at = created_at if created_at is not None else now
    e_at = expires_at if expires_at is not None else (now + timedelta(days=7))
    inv = Invitation(
        id=uuid.uuid4(),
        email=email,
        token_hash=token_hash,
        role=role,
        created_by=creator_id,
        created_at=c_at,
        expires_at=e_at,
        consumed_at=now if consumed else None,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(inv)
    return raw_token, inv


@pytest.mark.asyncio
async def test_registration_success_with_invitation_role() -> None:
    admin = await create_test_user(role="admin")
    now = datetime.now(timezone.utc)
    email = f"reg_success_{uuid.uuid4().hex[:8]}@example.com"
    raw_token, inv = await create_invitation_in_db(
        email=email, role="member", creator_id=admin.id, now=now
    )

    client_ip = f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        r = await client.post(
            "/api/v1/auth/register",
            json={
                "invitation_token": raw_token,
                "email": email,
                "password": "strong-password-123",
                "display_name": "New Registered Member",
            },
        )
        assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
        data = r.json()
        assert data["email"] == email
        assert data["role"] == "member"
        assert data["display_name"] == "New Registered Member"

    # Verify DB state directly
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        user_res = await session.execute(select(User).where(User.email == email))
        user_in_db = user_res.scalar_one()
        assert user_in_db.role == "member"
        assert verify_password("strong-password-123", user_in_db.password_hash)

        inv_res = await session.execute(select(Invitation).where(Invitation.id == inv.id))
        inv_in_db = inv_res.scalar_one()
        assert inv_in_db.consumed_at is not None


@pytest.mark.asyncio
async def test_role_injection_cannot_elevate_privileges() -> None:
    admin = await create_test_user(role="admin")
    now = datetime.now(timezone.utc)
    email = f"role_inj_{uuid.uuid4().hex[:8]}@example.com"
    raw_token, inv = await create_invitation_in_db(
        email=email, role="member", creator_id=admin.id, now=now
    )

    client_ip = f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        # Request body attempts role='admin', which must be rejected as an unknown field
        r = await client.post(
            "/api/v1/auth/register",
            json={
                "invitation_token": raw_token,
                "email": email,
                "password": "strong-password-123",
                "display_name": "Role Injection Attempt",
                "role": "admin",
            },
        )
        assert r.status_code == 422
        body = r.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"

    # Verify no user was created in the database for this email
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        user_res = await session.execute(select(User).where(User.email == email))
        assert user_res.scalar_one_or_none() is None

        inv_res = await session.execute(select(Invitation).where(Invitation.id == inv.id))
        inv_in_db = inv_res.scalar_one()
        assert inv_in_db.consumed_at is None


@pytest.mark.asyncio
async def test_registration_role_derives_from_invitation() -> None:
    admin = await create_test_user(role="admin")
    now = datetime.now(timezone.utc)

    member_email = f"role_member_{uuid.uuid4().hex[:8]}@example.com"
    member_token, _ = await create_invitation_in_db(
        email=member_email, role="member", creator_id=admin.id, now=now
    )

    admin_email = f"role_admin_{uuid.uuid4().hex[:8]}@example.com"
    admin_token, _ = await create_invitation_in_db(
        email=admin_email, role="admin", creator_id=admin.id, now=now
    )

    client_ip = f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        r_member = await client.post(
            "/api/v1/auth/register",
            json={
                "invitation_token": member_token,
                "email": member_email,
                "password": "strong-password-123",
                "display_name": "Member User",
            },
        )
        assert r_member.status_code == 201
        member_data = r_member.json()

        r_admin = await client.post(
            "/api/v1/auth/register",
            json={
                "invitation_token": admin_token,
                "email": admin_email,
                "password": "strong-password-123",
                "display_name": "Admin User",
            },
        )
        assert r_admin.status_code == 201
        admin_data = r_admin.json()

    assert member_data["role"] == "member"
    assert admin_data["role"] == "admin"
    assert member_data["role"] != admin_data["role"]

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        res_m = await session.execute(select(User).where(User.email == member_email))
        user_m = res_m.scalar_one()
        assert user_m.role == "member"

        res_a = await session.execute(select(User).where(User.email == admin_email))
        user_a = res_a.scalar_one()
        assert user_a.role == "admin"


@pytest.mark.asyncio
async def test_invalid_invitation_generic_422_identical_response() -> None:
    admin = await create_test_user(role="admin")
    now = datetime.now(timezone.utc)

    # 1. Non-existent token
    fake_token = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")

    # 2. Expired invitation
    email_expired = f"expired_{uuid.uuid4().hex[:8]}@example.com"
    expired_token, _ = await create_invitation_in_db(
        email=email_expired,
        role="member",
        creator_id=admin.id,
        now=now,
        created_at=now - timedelta(days=8),
        expires_at=now - timedelta(days=1),
    )

    # 3. Already-consumed invitation
    email_consumed = f"consumed_{uuid.uuid4().hex[:8]}@example.com"
    consumed_token, _ = await create_invitation_in_db(
        email=email_consumed,
        role="member",
        creator_id=admin.id,
        now=now,
        consumed=True,
    )

    # 4. Wrong email
    email_target = f"target_{uuid.uuid4().hex[:8]}@example.com"
    wrong_token, _ = await create_invitation_in_db(
        email=email_target,
        role="member",
        creator_id=admin.id,
        now=now,
    )

    test_cases = [
        (fake_token, f"fake_{uuid.uuid4().hex[:8]}@example.com"),
        (expired_token, email_expired),
        (consumed_token, email_consumed),
        (wrong_token, f"different_{uuid.uuid4().hex[:8]}@example.com"),
    ]

    client_ip = f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        responses = []
        for tok, em in test_cases:
            r = await client.post(
                "/api/v1/auth/register",
                json={
                    "invitation_token": tok,
                    "email": em,
                    "password": "strong-password-123",
                    "display_name": "Generic Error Test",
                },
            )
            assert r.status_code == 422
            responses.append(r.json())

        # Assert responses across all 4 invalid conditions are identical (no oracle)
        first_resp = responses[0]
        assert first_resp["error"]["code"] == "INVALID_INVITATION"
        for resp in responses[1:]:
            assert resp == first_resp


@pytest.mark.asyncio
async def test_invitation_single_use() -> None:
    admin = await create_test_user(role="admin")
    now = datetime.now(timezone.utc)
    email = f"single_use_{uuid.uuid4().hex[:8]}@example.com"
    raw_token, _ = await create_invitation_in_db(
        email=email, role="member", creator_id=admin.id, now=now
    )

    client_ip = f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        # First use succeeds
        r1 = await client.post(
            "/api/v1/auth/register",
            json={
                "invitation_token": raw_token,
                "email": email,
                "password": "strong-password-123",
                "display_name": "First Use",
            },
        )
        assert r1.status_code == 201

        # Second use with same token fails with 422
        r2 = await client.post(
            "/api/v1/auth/register",
            json={
                "invitation_token": raw_token,
                "email": email,
                "password": "strong-password-123",
                "display_name": "Second Use",
            },
        )
        assert r2.status_code == 422
        assert r2.json()["error"]["code"] == "INVALID_INVITATION"


@pytest.mark.asyncio
async def test_concurrent_registration_single_winner() -> None:
    admin = await create_test_user(role="admin")
    now = datetime.now(timezone.utc)
    email = f"concurrent_reg_{uuid.uuid4().hex[:8]}@example.com"
    raw_token, inv = await create_invitation_in_db(
        email=email, role="member", creator_id=admin.id, now=now
    )

    body = {
        "invitation_token": raw_token,
        "email": email,
        "password": "strong-password-123",
        "display_name": "Concurrent Winner",
    }

    client_ip = f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        r1, r2 = await asyncio.gather(
            client.post("/api/v1/auth/register", json=body),
            client.post("/api/v1/auth/register", json=body),
        )

    statuses = [r1.status_code, r2.status_code]
    assert sorted(statuses) == [201, 422]

    # Exactly one user in DB
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        count_res = await session.execute(select(func.count(User.id)).where(User.email == email))
        assert count_res.scalar_one() == 1


@pytest.mark.asyncio
async def test_registration_duplicate_email_409() -> None:
    admin = await create_test_user(role="admin")
    now = datetime.now(timezone.utc)
    existing_user = await create_test_user()

    raw_token, _ = await create_invitation_in_db(
        email=existing_user.email, role="member", creator_id=admin.id, now=now
    )

    client_ip = f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        r = await client.post(
            "/api/v1/auth/register",
            json={
                "invitation_token": raw_token,
                "email": existing_user.email,
                "password": "strong-password-123",
                "display_name": "Duplicate Attempt",
            },
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "EMAIL_EXISTS"


@pytest.mark.asyncio
async def test_admin_create_invitation_raw_token_never_persisted() -> None:
    admin = await create_test_user(role="admin")
    admin_token = make_admin_token(admin)
    target_email = f"e29_test_{uuid.uuid4().hex[:8]}@example.com"

    client_ip = f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        r = await client.post(
            "/api/v1/admin/invitations",
            json={"email": target_email, "role": "member"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 201
        data = r.json()
        assert "invitation_url" in data
        inv_url = data["invitation_url"]
        assert "#token=" in inv_url
        raw_token = inv_url.split("#token=")[1]
        inv_id = uuid.UUID(data["id"])

    # Query DB invitations table directly
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        inv_res = await session.execute(select(Invitation).where(Invitation.id == inv_id))
        inv_row = inv_res.scalar_one()

        # The raw token MUST NOT appear anywhere in the database row
        assert raw_token != inv_row.token_hash
        assert raw_token not in str(inv_row.token_hash)
        expected_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        assert inv_row.token_hash == expected_hash


@pytest.mark.asyncio
async def test_admin_create_invitation_cache_control_no_store() -> None:
    admin = await create_test_user(role="admin")
    admin_token = make_admin_token(admin)
    target_email = f"cc_test_{uuid.uuid4().hex[:8]}@example.com"

    client_ip = f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        r = await client.post(
            "/api/v1/admin/invitations",
            json={"email": target_email, "role": "member"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 201
        assert r.headers.get("Cache-Control") == "no-store"


@pytest.mark.asyncio
async def test_admin_create_invitation_permissions() -> None:
    admin = await create_test_user(role="admin")
    member = await create_test_user(role="member")
    admin_token = make_admin_token(admin)
    settings = get_settings()
    jwt_secret = settings.JWT_SECRET or "test-jwt-secret-minimum-32-bytes-long-12345678"
    now = datetime.now(timezone.utc)
    member_token = jwt.encode(
        {
            "sub": str(member.id),
            "role": "member",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=15)).timestamp()),
            "jti": str(uuid.uuid4()),
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
        },
        jwt_secret,
        algorithm="HS256",
    )

    client_ip = f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    body = {"email": f"perm_test_{uuid.uuid4().hex[:8]}@example.com", "role": "member"}
    async with make_client(ip=client_ip) as client:
        # Anonymous -> 401
        r_anon = await client.post("/api/v1/admin/invitations", json=body)
        assert r_anon.status_code == 401

        # Member -> 403
        r_mem = await client.post(
            "/api/v1/admin/invitations",
            json=body,
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r_mem.status_code == 403

        # Admin -> 201
        r_adm = await client.post(
            "/api/v1/admin/invitations",
            json=body,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_adm.status_code == 201


@pytest.mark.asyncio
async def test_audit_rows_for_registration_and_invitation_no_secrets() -> None:
    admin = await create_test_user(role="admin")
    admin_token = make_admin_token(admin)
    target_email = f"audit_sec_{uuid.uuid4().hex[:8]}@example.com"

    client_ip = f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        # 1. Create invitation
        r_inv = await client.post(
            "/api/v1/admin/invitations",
            json={"email": target_email, "role": "member"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_inv.status_code == 201
        inv_data = r_inv.json()
        raw_token = inv_data["invitation_url"].split("#token=")[1]

        # 2. Register with invitation
        r_reg = await client.post(
            "/api/v1/auth/register",
            json={
                "invitation_token": raw_token,
                "email": target_email,
                "password": "strong-password-123",
                "display_name": "Audit Security Test",
            },
        )
        assert r_reg.status_code == 201

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Verify invitation audit row
        inv_audit_res = await session.execute(
            select(AuditLog).where(
                AuditLog.action == "admin.invitation_create",
                AuditLog.target_id == uuid.UUID(inv_data["id"]),
            )
        )
        inv_audit = inv_audit_res.scalar_one()
        inv_details_str = str(inv_audit.details)
        assert raw_token not in inv_details_str
        assert "token" not in inv_audit.details
        assert "invitation_url" not in inv_audit.details

        # Verify registration audit row
        reg_audit_res = await session.execute(
            select(AuditLog).where(
                AuditLog.action == "auth.register",
                AuditLog.actor_id == uuid.UUID(r_reg.json()["id"]),
            )
        )
        reg_audit = reg_audit_res.scalar_one()
        reg_details_str = str(reg_audit.details)
        assert raw_token not in reg_details_str
        assert "password" not in reg_details_str
        assert "strong-password-123" not in reg_details_str


@pytest.mark.asyncio
async def test_refresh_reuse_audit_committed_and_visible_from_separate_connection() -> None:
    password = "valid-password-123"
    user = await create_test_user(password=password, role="member", enabled=True)

    client_ip = f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        # Login to obtain initial refresh cookie
        r_login = await client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": password},
        )
        assert r_login.status_code == 200
        first_rt = client.cookies.get("commonsbook_rt")
        assert first_rt is not None

        # First refresh: succeeds, rotates token (first_rt is now marked used)
        r_ref1 = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert r_ref1.status_code == 200

        # Reuse attempt: replay first_rt
        client.cookies.set("commonsbook_rt", first_rt)
        r_reuse = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert r_reuse.status_code == 401
        assert r_reuse.json()["error"]["code"] == "INVALID_REFRESH"

    # Verify audit row in a fresh, SEPARATE session / connection
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh_session:
        audit_stmt = select(AuditLog).where(
            AuditLog.action == "auth.refresh_reuse",
            AuditLog.actor_id == user.id,
        )
        audit_res = await fresh_session.execute(audit_stmt)
        audit_row = audit_res.scalar_one_or_none()
        assert audit_row is not None
        assert audit_row.details.get("event_category") == "refresh_reuse"
        assert audit_row.details.get("user_id") == str(user.id)
        assert "family_id" in audit_row.details
        assert first_rt not in str(audit_row.details)


@pytest.mark.asyncio
async def test_seed_cli_refuses_when_user_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from scripts.seed import run_seed

    # Ensure at least one user exists
    await create_test_user()

    # Provide fake password input
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "admin-password-123")

    with pytest.raises(SystemExit) as exc_info:
        await run_seed(email="seed_admin@example.com", display_name="Initial Admin")
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_reset_password_cli_dry_run_and_approval_enforcement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from scripts.reset_password import run_reset

    user = await create_test_user(password="initial-password-123")
    initial_hash = user.password_hash

    # Add an active refresh token
    now = datetime.now(timezone.utc)
    raw_bytes = os.urandom(32)
    raw_refresh = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
    rt = RefreshToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=hashlib.sha256(raw_refresh.encode("utf-8")).hexdigest(),
        family_id=uuid.uuid4(),
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

    new_pwd = "brand-new-password-123"
    monkeypatch.setattr("getpass.getpass", lambda prompt="": new_pwd)

    # 1. Dry run: writes nothing
    await run_reset(user_id_str=str(user.id), apply=False, approval_file=None)
    async with sessionmaker() as session:
        u_res = await session.execute(select(User).where(User.id == user.id))
        u = u_res.scalar_one()
        assert u.password_hash == initial_hash
        assert u.version == 1

    # 2. Apply without approval file -> refuses
    with pytest.raises(SystemExit) as exc_info:
        await run_reset(user_id_str=str(user.id), apply=True, approval_file=None)
    assert exc_info.value.code == 1

    # 3. Apply with mismatched approval file -> refuses
    bad_approval_file = tmp_path / "bad_approval.txt"  # type: ignore[operator]
    bad_approval_file.write_text(str(uuid.uuid4()), encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        await run_reset(
            user_id_str=str(user.id),
            apply=True,
            approval_file=str(bad_approval_file),
        )
    assert exc_info.value.code == 1

    # 4. Apply with matching approval file -> succeeds
    good_approval_file = tmp_path / "good_approval.txt"  # type: ignore[operator]
    good_approval_file.write_text(str(user.id), encoding="utf-8")
    await run_reset(
        user_id_str=str(user.id),
        apply=True,
        approval_file=str(good_approval_file),
    )

    # Verify user hash changed and version incremented
    async with sessionmaker() as session:
        u_res = await session.execute(select(User).where(User.id == user.id))
        u = u_res.scalar_one()
        assert u.password_hash != initial_hash
        assert verify_password(new_pwd, u.password_hash)
        assert u.version == 2

        # Verify refresh token revoked
        rt_res = await session.execute(select(RefreshToken).where(RefreshToken.id == rt.id))
        rt_db = rt_res.scalar_one()
        assert rt_db.revoked_at is not None
