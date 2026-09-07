import asyncio
import base64
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.auth.dependencies import InvalidRefreshError
from app.auth.models import RefreshToken, User
from app.auth.passwords import hash_password
from app.auth.service import rotate_refresh
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app

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


async def create_test_user(
    *,
    email: str | None = None,
    password: str = "valid-password-123",
    role: str = "member",
    enabled: bool = True,
    display_name: str = "Test Refresh User",
) -> User:
    if email is None:
        email = f"refresh_{uuid.uuid4().hex[:8]}@example.com"

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


async def issue_test_refresh_token(
    user: User,
    *,
    family_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    family_expires_at: datetime | None = None,
    used_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> tuple[str, RefreshToken]:
    now = datetime.now(timezone.utc)
    raw_bytes = os.urandom(32)
    raw_refresh = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
    token_hash = hashlib.sha256(raw_refresh.encode("utf-8")).hexdigest()

    if family_id is None:
        family_id = uuid.uuid4()
    if expires_at is None:
        expires_at = now + timedelta(days=7)
    if family_expires_at is None:
        family_expires_at = max(expires_at, now + timedelta(days=30))
    if created_at is None:
        created_at = expires_at - timedelta(days=7)

    token_record = RefreshToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=token_hash,
        family_id=family_id,
        parent_id=parent_id,
        created_at=created_at,
        expires_at=expires_at,
        family_expires_at=family_expires_at,
        used_at=used_at,
        revoked_at=revoked_at,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(token_record)

    return raw_refresh, token_record


def create_client(
    ip: str | None = None, cookies: dict[str, str] | None = None
) -> httpx.AsyncClient:
    if ip is None:
        ip = f"10.6.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 12345)),
        base_url="http://localhost:5173",
        cookies=cookies,
    )


@pytest.mark.asyncio
async def test_happy_rotation() -> None:
    """Happy rotation: 200, new cookie differs from old, old token used, exactly one child."""
    user = await create_test_user()
    raw_old, old_token = await issue_test_refresh_token(user)

    async with create_client(cookies={"commonsbook_rt": raw_old}) as client:
        resp = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "no-store"
        assert "X-Request-ID" in resp.headers

        data = resp.json()
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == 900
        assert isinstance(data["access_token"], str) and len(data["access_token"]) > 20
        assert data["user"]["id"] == str(user.id)

        # Cookie check
        cookie_header = resp.headers.get("set-cookie")
        assert cookie_header is not None
        assert "commonsbook_rt=" in cookie_header
        new_cookie_val = resp.cookies.get("commonsbook_rt")
        assert new_cookie_val is not None
        assert new_cookie_val != raw_old

    # Direct DB assertion from separate session
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # Check old token
        stmt = select(RefreshToken).where(RefreshToken.id == old_token.id)
        res = await session.execute(stmt)
        old_db = res.scalar_one()
        assert old_db.used_at is not None
        assert old_db.revoked_at is None

        # Check child token
        stmt_child = select(RefreshToken).where(RefreshToken.parent_id == old_token.id)
        res_child = await session.execute(stmt_child)
        children = res_child.scalars().all()
        assert len(children) == 1
        child = children[0]
        assert child.family_id == old_token.family_id
        assert child.user_id == user.id
        assert child.used_at is None
        assert child.revoked_at is None
        expected_hash = hashlib.sha256(new_cookie_val.encode("utf-8")).hexdigest()
        assert child.token_hash == expected_hash
        assert child.family_expires_at == old_token.family_expires_at


@pytest.mark.asyncio
async def test_reuse_detection_revokes_entire_family_and_commits() -> None:
    """REUSE DETECTION: Replaying a used token revokes the entire family,

    commits the revocation, and returns 401 INVALID_REFRESH.
    """
    user = await create_test_user()
    raw_old, old_token = await issue_test_refresh_token(user)

    async with create_client(cookies={"commonsbook_rt": raw_old}) as client:
        # 1. First rotation succeeds
        resp1 = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp1.status_code == 200

        # 2. Replay the SAME raw token (reuse)
        client.cookies.set("commonsbook_rt", raw_old)
        resp2 = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp2.status_code == 401
        err = resp2.json()["error"]
        assert err["code"] == "INVALID_REFRESH"

    # 3. Direct DB query from a separate session observes COMMITTED family revocation
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as separate_session:
        stmt = select(RefreshToken).where(RefreshToken.family_id == old_token.family_id)
        res = await separate_session.execute(stmt)
        family_tokens = res.scalars().all()
        assert len(family_tokens) == 2  # parent and child
        for tok in family_tokens:
            assert tok.revoked_at is not None, f"Token {tok.id} was not revoked on reuse!"


@pytest.mark.asyncio
async def test_child_issued_before_reuse_cannot_rotate() -> None:
    """The child issued before a reuse is also revoked and cannot subsequently rotate."""
    user = await create_test_user()
    raw_old, old_token = await issue_test_refresh_token(user)

    async with create_client(cookies={"commonsbook_rt": raw_old}) as client:
        # Initial rotation: gets child token
        resp1 = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp1.status_code == 200
        raw_child = resp1.cookies.get("commonsbook_rt")
        assert raw_child is not None

        # Attacker replays raw_old
        client.cookies.set("commonsbook_rt", raw_old)
        resp_reuse = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp_reuse.status_code == 401

        # Legitimate client tries to use raw_child: must be rejected 401
        client.cookies.set("commonsbook_rt", raw_child)
        resp_child = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp_child.status_code == 401
        assert resp_child.json()["error"]["code"] == "INVALID_REFRESH"


@pytest.mark.asyncio
async def test_expired_token_rejected() -> None:
    """Expired token (expires_at in the past) returns 401 INVALID_REFRESH."""
    user = await create_test_user()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    raw_expired, _ = await issue_test_refresh_token(user, expires_at=past)

    async with create_client(cookies={"commonsbook_rt": raw_expired}) as client:
        resp = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_REFRESH"


@pytest.mark.asyncio
async def test_family_cap_expired_rejected() -> None:
    """Family cap (family_expires_at passed) returns 401 INVALID_REFRESH."""
    user = await create_test_user()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    raw_cap_expired, _ = await issue_test_refresh_token(
        user, expires_at=past, family_expires_at=past
    )

    async with create_client(cookies={"commonsbook_rt": raw_cap_expired}) as client:
        resp = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_REFRESH"


@pytest.mark.asyncio
async def test_disable_versus_refresh_race() -> None:
    """Disable the user and assert no token is minted afterwards."""
    user = await create_test_user(enabled=True)
    raw_token, token_rec = await issue_test_refresh_token(user)

    # Disable user taking lock and committing
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            stmt = select(User).where(User.id == user.id).with_for_update()
            res = await session.execute(stmt)
            locked_u = res.scalar_one()
            locked_u.enabled = False

    async with create_client(cookies={"commonsbook_rt": raw_token}) as client:
        resp = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_REFRESH"

    # Assert no child token was minted
    async with sessionmaker() as session:
        stmt_child = select(RefreshToken).where(RefreshToken.parent_id == token_rec.id)
        res_child = await session.execute(stmt_child)
        assert len(res_child.scalars().all()) == 0


@pytest.mark.asyncio
async def test_concurrent_rotation_of_same_token() -> None:
    """Concurrent rotation of the SAME token: exactly one succeeds and one child is minted."""
    user = await create_test_user()
    raw_token, token_rec = await issue_test_refresh_token(user)

    ip = f"10.7.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"

    async def do_rotate() -> httpx.Response:
        async with create_client(ip=ip, cookies={"commonsbook_rt": raw_token}) as client:
            return await client.post(
                "/api/v1/auth/refresh",
                headers={"Origin": "http://localhost:5173"},
            )

    resp1, resp2 = await asyncio.gather(do_rotate(), do_rotate())
    status_codes = [resp1.status_code, resp2.status_code]
    assert 200 in status_codes
    assert status_codes.count(200) == 1

    # Database invariant: unique partial index on parent_id guarantees exactly one child
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt_child = select(RefreshToken).where(RefreshToken.parent_id == token_rec.id)
        res_child = await session.execute(stmt_child)
        children = res_child.scalars().all()
        assert len(children) == 1


@pytest.mark.asyncio
async def test_cookie_attributes_on_rotated_cookie() -> None:
    """Rotated cookie has HttpOnly, SameSite=Lax, and Path=/api/v1/auth."""
    user = await create_test_user()
    raw_token, _ = await issue_test_refresh_token(user)

    async with create_client(cookies={"commonsbook_rt": raw_token}) as client:
        resp = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp.status_code == 200

        cookie_header = resp.headers.get("set-cookie")
        assert cookie_header is not None
        assert "commonsbook_rt=" in cookie_header
        assert "HttpOnly" in cookie_header
        assert "Path=/api/v1/auth" in cookie_header
        assert "samesite=lax" in cookie_header.lower()


@pytest.mark.asyncio
async def test_exact_origin_enforcement_refresh() -> None:
    """Origin enforcement on refresh: absent, wrong, and near-miss origins produce 403."""
    user = await create_test_user()
    raw_token, _ = await issue_test_refresh_token(user)

    near_misses = [
        None,  # Absent Origin
        "http://evil.com",  # Completely wrong
        "http://localhost:5173.attacker.com",  # Prefix match near-miss
        "http://attacker-localhost:5173",  # Suffix match near-miss
        "http://localhost:5173/",  # Trailing slash
        "https://localhost:5173",  # Scheme mismatch
        "http://localhost:5174",  # Port mismatch
        "HTTP://LOCALHOST:5173",  # Casing difference
    ]

    for bad_origin in near_misses:
        headers = {}
        if bad_origin is not None:
            headers["Origin"] = bad_origin

        async with create_client(cookies={"commonsbook_rt": raw_token}) as client:
            resp = await client.post(
                "/api/v1/auth/refresh",
                headers=headers,
            )
            assert resp.status_code == 403, (
                f"Expected 403 for Origin '{bad_origin}', got {resp.status_code}"
            )
            assert resp.json()["error"]["code"] == "ORIGIN_REJECTED"


@pytest.mark.asyncio
async def test_logout_origin_and_absent_cookie() -> None:
    """E04 with absent cookie but valid Origin returns 204; absent Origin returns 403."""
    async with create_client() as client:
        # Valid Origin, absent cookie -> 204
        resp_valid = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp_valid.status_code == 204
        cookie_header = resp_valid.headers.get("set-cookie")
        assert cookie_header is not None
        assert "commonsbook_rt=" in cookie_header
        assert "Max-Age=0" in cookie_header or "expires=" in cookie_header

        # Absent Origin -> 403
        resp_no_origin = await client.post("/api/v1/auth/logout")
        assert resp_no_origin.status_code == 403
        assert resp_no_origin.json()["error"]["code"] == "ORIGIN_REJECTED"

        # Near-miss Origin -> 403
        resp_bad_origin = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "http://localhost:5173.attacker.com"},
        )
        assert resp_bad_origin.status_code == 403
        assert resp_bad_origin.json()["error"]["code"] == "ORIGIN_REJECTED"


@pytest.mark.asyncio
async def test_logout_twice_idempotent() -> None:
    """E04 called twice in a row returns 204 both times."""
    user = await create_test_user()
    raw_token, token_rec = await issue_test_refresh_token(user)

    async with create_client(cookies={"commonsbook_rt": raw_token}) as client:
        # First logout
        resp1 = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp1.status_code == 204

        # Verify family revoked in DB
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            stmt = select(RefreshToken).where(RefreshToken.id == token_rec.id)
            res = await session.execute(stmt)
            tok_db = res.scalar_one()
            assert tok_db.revoked_at is not None

        # Second logout with same cookie
        client.cookies.set("commonsbook_rt", raw_token)
        resp2 = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp2.status_code == 204


@pytest.mark.asyncio
async def test_raw_refresh_token_never_in_json_body() -> None:
    """The raw refresh token appears in NO JSON response body."""
    user = await create_test_user()
    raw_token, _ = await issue_test_refresh_token(user)

    async with create_client(cookies={"commonsbook_rt": raw_token}) as client:
        resp = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp.status_code == 200
        # Assert old raw token is not in response text
        assert raw_token not in resp.text
        # Assert new raw token is not in response text
        new_token = resp.cookies.get("commonsbook_rt")
        assert new_token is not None
        assert new_token not in resp.text

        # Logout response body
        client.cookies.set("commonsbook_rt", new_token)
        resp_logout = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp_logout.status_code == 204
        assert len(resp_logout.content) == 0


@pytest.mark.asyncio
async def test_reuse_detection_completes_under_lock_timeout() -> None:
    """Replaying a used token must complete well under the 15s database lock timeout."""
    user = await create_test_user()
    raw_old, old_token = await issue_test_refresh_token(user)

    async with create_client(cookies={"commonsbook_rt": raw_old}) as client:
        # Initial rotation succeeds
        resp1 = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp1.status_code == 200

        # Replay the same raw token (reuse detection)
        client.cookies.set("commonsbook_rt", raw_old)
        start_time = asyncio.get_running_loop().time()
        resp2 = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        elapsed = asyncio.get_running_loop().time() - start_time

        assert resp2.status_code == 401
        assert resp2.json()["error"]["code"] == "INVALID_REFRESH"
        assert elapsed < 5.0, f"Reuse path took {elapsed:.2f}s, expected < 5.0s"

    # Direct DB assertion from separate session proves family revocation committed
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as separate_session:
        stmt = select(RefreshToken).where(RefreshToken.family_id == old_token.family_id)
        res = await separate_session.execute(stmt)
        tokens = res.scalars().all()
        assert len(tokens) == 2
        for tok in tokens:
            assert tok.revoked_at is not None


@pytest.mark.asyncio
async def test_reuse_detection_does_not_commit_caller_pending_work() -> None:
    """Service rotate_refresh does not commit caller's pending transaction work on reuse."""
    user = await create_test_user()
    raw_old, old_token = await issue_test_refresh_token(user)

    # Initial rotation so token becomes used
    async with create_client(cookies={"commonsbook_rt": raw_old}) as client:
        resp1 = await client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp1.status_code == 200

    canary_email = f"canary_{uuid.uuid4().hex[:8]}@example.com"
    canary_user = User(
        id=uuid.uuid4(),
        email=canary_email,
        password_hash="dummy-hash-password",
        display_name="Canary User",
        role="member",
        enabled=True,
        version=1,
    )

    now = datetime.now(timezone.utc)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as caller_session:
        with pytest.raises(InvalidRefreshError):
            async with caller_session.begin():
                caller_session.add(canary_user)
                await caller_session.flush()
                await rotate_refresh(caller_session, raw_token=raw_old, now=now)

    # Verify from separate session that canary_user was NOT committed
    async with sessionmaker() as separate_session:
        stmt = select(User).where(User.email == canary_email)
        res = await separate_session.execute(stmt)
        assert res.scalar_one_or_none() is None

        # Confirm family revocation was committed despite caller transaction rollback
        stmt_tokens = select(RefreshToken).where(RefreshToken.family_id == old_token.family_id)
        res_tokens = await separate_session.execute(stmt_tokens)
        tokens = res_tokens.scalars().all()
        assert len(tokens) == 2
        for tok in tokens:
            assert tok.revoked_at is not None
