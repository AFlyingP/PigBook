import asyncio
import base64
import hashlib
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
from app.auth.models import RefreshToken, User
from app.auth.passwords import hash_password
from app.bookings.models import Booking
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


async def create_user(
    *,
    email: str | None = None,
    password: str = "valid-password-123",
    display_name: str = "Test User",
    role: str = "member",
    enabled: bool = True,
    created_at: datetime | None = None,
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
    if created_at is not None:
        user.created_at = created_at
        user.updated_at = created_at

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(user)
    return user


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


@pytest.mark.asyncio
async def test_admin_list_users_pagination_and_filter() -> None:
    """E27: GET /api/v1/admin/users pagination, filter, and stable order."""
    now = datetime.now(timezone.utc)
    admin = await create_user(role="admin", enabled=True, created_at=now)
    admin_token = make_token(admin)

    # Create users with specific created_at timestamps
    await create_user(role="member", enabled=True, created_at=now - timedelta(seconds=30))
    await create_user(role="member", enabled=False, created_at=now - timedelta(seconds=20))
    await create_user(role="member", enabled=True, created_at=now - timedelta(seconds=10))

    client_ip = f"10.8.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        # All users
        r = await client.get(
            "/api/v1/admin/users?limit=10&offset=0",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert data["total"] >= 4
        # Verify ordering: created_at descending
        items = data["items"]
        timestamps = [item["created_at"] for item in items]
        assert timestamps == sorted(timestamps, reverse=True)

        # Filter enabled=true
        r_en = await client.get(
            "/api/v1/admin/users?enabled=true",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_en.status_code == 200
        for item in r_en.json()["items"]:
            assert item["enabled"] is True

        # Filter enabled=false
        r_dis = await client.get(
            "/api/v1/admin/users?enabled=false",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_dis.status_code == 200
        for item in r_dis.json()["items"]:
            assert item["enabled"] is False


@pytest.mark.asyncio
async def test_user_patch_validation_and_preconditions() -> None:
    """E28: If-Match header precedence (428, 422, 412) and UserPatch body validation."""
    admin = await create_user(role="admin", enabled=True)
    admin_token = make_token(admin)
    target = await create_user(role="member", enabled=True)

    client_ip = f"10.8.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        # 1. Missing If-Match -> 428 PRECONDITION_REQUIRED
        r_no_if = await client.patch(
            f"/api/v1/admin/users/{target.id}",
            json={"role": "admin"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_no_if.status_code == 428
        assert r_no_if.json()["error"]["code"] == "PRECONDITION_REQUIRED"

        # 2. Malformed If-Match -> 422 VALIDATION_ERROR (checked before target lookup)
        r_bad_if = await client.patch(
            f"/api/v1/admin/users/{target.id}",
            json={"role": "admin"},
            headers={"Authorization": f"Bearer {admin_token}", "If-Match": "invalid-etag"},
        )
        assert r_bad_if.status_code == 422
        assert r_bad_if.json()["error"]["code"] == "VALIDATION_ERROR"

        # 3. Empty UserPatch body -> 422 VALIDATION_ERROR
        r_empty = await client.patch(
            f"/api/v1/admin/users/{target.id}",
            json={},
            headers={"Authorization": f"Bearer {admin_token}", "If-Match": f'"{target.version}"'},
        )
        assert r_empty.status_code == 422

        # 4. Null values in UserPatch -> 422 VALIDATION_ERROR
        r_null = await client.patch(
            f"/api/v1/admin/users/{target.id}",
            json={"role": None},
            headers={"Authorization": f"Bearer {admin_token}", "If-Match": f'"{target.version}"'},
        )
        assert r_null.status_code == 422

        # 5. Stale version -> 412 VERSION_MISMATCH
        r_stale = await client.patch(
            f"/api/v1/admin/users/{target.id}",
            json={"role": "admin"},
            headers={
                "Authorization": f"Bearer {admin_token}",
                "If-Match": f'"{target.version + 99}"',
            },
        )
        assert r_stale.status_code == 412
        assert r_stale.json()["error"]["code"] == "VERSION_MISMATCH"


@pytest.mark.asyncio
async def test_last_admin_rejection_and_self_demotion_with_another_admin() -> None:
    """E28: Last-admin rule (409 LAST_ADMIN) and successful demotion when another admin remains."""
    admin1 = await create_user(role="admin", enabled=True)
    admin1_token = make_token(admin1)

    client_ip = f"10.8.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            count_stmt = select(User).where(User.role == "admin", User.enabled.is_(True))
            admins = (await session.execute(count_stmt)).scalars().all()

        if len(admins) == 1:
            r_demote_last = await client.patch(
                f"/api/v1/admin/users/{admin1.id}",
                json={"role": "member"},
                headers={
                    "Authorization": f"Bearer {admin1_token}",
                    "If-Match": f'"{admin1.version}"',
                },
            )
            assert r_demote_last.status_code == 409
            assert r_demote_last.json()["error"]["code"] == "LAST_ADMIN"

            r_disable_last = await client.patch(
                f"/api/v1/admin/users/{admin1.id}",
                json={"enabled": False},
                headers={
                    "Authorization": f"Bearer {admin1_token}",
                    "If-Match": f'"{admin1.version}"',
                },
            )
            assert r_disable_last.status_code == 409
            assert r_disable_last.json()["error"]["code"] == "LAST_ADMIN"

        # Now add second admin
        await create_user(role="admin", enabled=True)

        # Self-demotion of admin1 while another admin remains: Allowed!
        r_demote = await client.patch(
            f"/api/v1/admin/users/{admin1.id}",
            json={"role": "member"},
            headers={"Authorization": f"Bearer {admin1_token}", "If-Match": f'"{admin1.version}"'},
        )
        assert r_demote.status_code == 200
        assert r_demote.json()["role"] == "member"
        assert r_demote.headers.get("ETag") == f'"{admin1.version + 1}"'

        # Now admin1 has role member. Its old token will fail with 403 on D routes
        # because the database is reloaded on every request!
        r_stale_priv = await client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin1_token}"},
        )
        assert r_stale_priv.status_code == 403
        assert r_stale_priv.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_concurrent_two_admin_demotion() -> None:
    """E28: Concurrent demotion of two admins serializes on advisory lock 714001.

    Exactly one succeeds and the other fails with 409 LAST_ADMIN.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            existing_admins_stmt = select(User).where(User.role == "admin", User.enabled.is_(True))
            existing = (await session.execute(existing_admins_stmt)).scalars().all()
            for u in existing:
                u.enabled = False

    admin_a = await create_user(role="admin", enabled=True)
    admin_b = await create_user(role="admin", enabled=True)
    token_a = make_token(admin_a)
    token_b = make_token(admin_b)

    client_ip_a = f"10.8.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    client_ip_b = f"10.8.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"

    async with make_client(ip=client_ip_a) as client_a, make_client(ip=client_ip_b) as client_b:
        task_a = client_a.patch(
            f"/api/v1/admin/users/{admin_a.id}",
            json={"role": "member"},
            headers={"Authorization": f"Bearer {token_a}", "If-Match": f'"{admin_a.version}"'},
        )
        task_b = client_b.patch(
            f"/api/v1/admin/users/{admin_b.id}",
            json={"role": "member"},
            headers={"Authorization": f"Bearer {token_b}", "If-Match": f'"{admin_b.version}"'},
        )

        responses = await asyncio.gather(task_a, task_b)
        status_codes = sorted([r.status_code for r in responses])
        assert status_codes == [200, 409], f"Unexpected status codes: {status_codes}"

        success_resp = next(r for r in responses if r.status_code == 200)
        conflict_resp = next(r for r in responses if r.status_code == 409)

        assert conflict_resp.json()["error"]["code"] == "LAST_ADMIN"
        assert success_resp.json()["role"] == "member"


@pytest.mark.asyncio
async def test_disabling_user_revokes_refresh_families_and_preserves_history() -> None:
    """E28: Disabling user revokes all refresh families and audits mutation,

    while preserving bookings and waitlist entries.
    """
    admin = await create_user(role="admin", enabled=True)
    admin_token = make_token(admin)

    user = await create_user(role="member", enabled=True)
    now = datetime.now(timezone.utc)

    # Create refresh token for user
    await create_refresh_token(user, now)

    # Create a resource and a booking for user to verify preservation
    sessionmaker = get_sessionmaker()
    resource_id = uuid.uuid4()
    booking_id = uuid.uuid4()
    async with sessionmaker() as session:
        async with session.begin():
            r = Resource(
                id=resource_id,
                name="Room Preservation",
                description="desc",
                location="Loc",
                active=True,
                version=1,
            )
            session.add(r)
            await session.flush()
            b = Booking(
                id=booking_id,
                resource_id=resource_id,
                user_id=user.id,
                created_by=user.id,
                kind="reservation",
                time_range=Range(
                    now + timedelta(days=2), now + timedelta(days=2, hours=1), bounds="[)"
                ),
                status="confirmed",
                version=1,
            )
            session.add(b)
            await session.flush()

    client_ip = f"10.8.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        r_disable = await client.patch(
            f"/api/v1/admin/users/{user.id}",
            json={"enabled": False},
            headers={"Authorization": f"Bearer {admin_token}", "If-Match": f'"{user.version}"'},
        )
        assert r_disable.status_code == 200
        assert r_disable.json()["enabled"] is False

        # Verify refresh tokens are revoked
        async with sessionmaker() as session:
            stmt = select(RefreshToken).where(RefreshToken.user_id == user.id)
            tokens = (await session.execute(stmt)).scalars().all()
            assert len(tokens) > 0
            for t in tokens:
                assert t.revoked_at is not None

            # Verify booking is preserved intact
            b_stmt = select(Booking).where(Booking.id == booking_id)
            saved_b = (await session.execute(b_stmt)).scalar_one_or_none()
            assert saved_b is not None
            assert saved_b.status == "confirmed"
            assert saved_b.user_id == user.id

            # Verify audit log recorded
            audit_stmt = select(AuditLog).where(
                AuditLog.target_type == "user",
                AuditLog.target_id == user.id,
                AuditLog.action == "admin.user_update",
            )
            audit_entry = (await session.execute(audit_stmt)).scalar_one_or_none()
            assert audit_entry is not None
            assert audit_entry.details["fields"] == ["enabled"]
            assert audit_entry.details["old_enabled"] is True
            assert audit_entry.details["new_enabled"] is False


@pytest.mark.asyncio
async def test_close_o001_disable_user_holding_used_token_and_live_sibling() -> None:
    """Close O-001: Disabling a user holding a used token plus a live sibling

    revokes all tokens in the family so no live family remains, and subsequent
    replay of the used token cannot rotate.
    """
    admin = await create_user(role="admin", enabled=True)
    admin_token = make_token(admin)

    user = await create_user(role="member", enabled=True)
    now = datetime.now(timezone.utc)

    client_ip = f"10.8.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    # 1. User logs in / mints refresh token (Token 1)
    raw_token_1 = await create_refresh_token(user, now)

    # 2. Rotate Token 1 -> Token 1 becomes used, Token 2 is minted in same family
    raw_token_2: str | None = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(client_ip, 12345)),
        base_url="http://localhost:5173",
        cookies={"commonsbook_rt": raw_token_1},
    ) as rotate_client:
        r_rot = await rotate_client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert r_rot.status_code == 200
        raw_token_2 = r_rot.cookies.get("commonsbook_rt")
        assert raw_token_2 is not None
        assert raw_token_2 != raw_token_1

    # Confirm DB state: Token 1 is used, Token 2 is live sibling (revoked_at is NULL)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(RefreshToken).where(RefreshToken.user_id == user.id)
        tokens = (await session.execute(stmt)).scalars().all()
        assert len(tokens) == 2
        t1 = next(t for t in tokens if t.used_at is not None)
        t2 = next(t for t in tokens if t.used_at is None)
        assert t1.revoked_at is None
        assert t2.revoked_at is None

    # 3. Administrator disables user via E28 PATCH /api/v1/admin/users/{id}
    async with make_client(ip=client_ip) as admin_client:
        r_dis = await admin_client.patch(
            f"/api/v1/admin/users/{user.id}",
            json={"enabled": False},
            headers={"Authorization": f"Bearer {admin_token}", "If-Match": f'"{user.version}"'},
        )
        assert r_dis.status_code == 200
        assert r_dis.json()["enabled"] is False

    # 4. Assert in DB: NO live token or family remains for disabled user
    async with sessionmaker() as session:
        stmt_after = select(RefreshToken).where(RefreshToken.user_id == user.id)
        tokens_after = (await session.execute(stmt_after)).scalars().all()
        for t in tokens_after:
            assert t.revoked_at is not None, (
                f"Token {t.id} in family {t.family_id} was not revoked on disable!"
            )

    # 5. Subsequent replay of used Token 1 cannot rotate
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(client_ip, 12345)),
        base_url="http://localhost:5173",
        cookies={"commonsbook_rt": raw_token_1},
    ) as replay_client:
        r_replay = await replay_client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert r_replay.status_code == 401

    # 6. Presentation of sibling Token 2 also cannot rotate
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(client_ip, 12345)),
        base_url="http://localhost:5173",
        cookies={"commonsbook_rt": raw_token_2},
    ) as sibling_client:
        r_sibling = await sibling_client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert r_sibling.status_code == 401
