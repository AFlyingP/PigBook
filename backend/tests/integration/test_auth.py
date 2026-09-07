import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from sqlalchemy import select

# Ensure test secrets are set before importing app components
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.auth.models import RateLimit, User
from app.auth.passwords import hash_password, normalize_email
from app.auth.rate_limit import hash_identity
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


async def create_db_user(
    *,
    email: str | None = None,
    password: str = "valid-password-123",
    role: str = "member",
    enabled: bool = True,
    display_name: str = "Test User",
) -> User:
    """Helper to insert and commit a user to the database."""
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


def create_client(ip: str = "127.0.0.1", port: int = 12345) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, port)),
        base_url="http://localhost:5173",
    )


@pytest.mark.asyncio
async def test_login_success() -> None:
    """Successful login returns 200, TokenResponse with token_type 'bearer' and

    expires_in 900, sets the refresh cookie with correct name/flags/path, and sends
    Cache-Control: no-store.
    """
    password = "super-secret-password-123"
    user = await create_db_user(password=password)

    client_ip = f"10.1.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with create_client(ip=client_ip) as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": password},
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
        assert data["user"]["email"] == user.email
        assert data["user"]["role"] == user.role
        assert data["user"]["enabled"] is True

        # In APP_ENV=local/test, cookie name is commonsbook_rt with Secure=False
        cookie_header = resp.headers.get("set-cookie")
        assert cookie_header is not None
        assert "commonsbook_rt=" in cookie_header
        assert "HttpOnly" in cookie_header
        assert "Path=/api/v1/auth" in cookie_header
        assert "samesite=lax" in cookie_header.lower()


@pytest.mark.asyncio
async def test_login_generic_failure_response() -> None:
    """Wrong password and unknown email return the SAME generic 401 INVALID_CREDENTIALS body."""
    password = "super-secret-password-123"
    user = await create_db_user(password=password)

    client_ip = f"10.2.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with create_client(ip=client_ip) as client:
        # Wrong password
        resp_wrong_pw = await client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": "wrong-password-456"},
        )
        assert resp_wrong_pw.status_code == 401

        # Unknown email
        resp_unknown_email = await client.post(
            "/api/v1/auth/login",
            json={"email": "nonexistent-user@example.com", "password": "any-password-123"},
        )
        assert resp_unknown_email.status_code == 401

        # Assert identical error response envelope and codes
        assert resp_wrong_pw.json() == {
            "error": {
                "code": "INVALID_CREDENTIALS",
                "message": "Invalid email or password",
                "details": {},
            }
        }
        assert resp_wrong_pw.json() == resp_unknown_email.json()


@pytest.mark.asyncio
async def test_login_disabled_user_cannot_login() -> None:
    """A disabled user cannot log in."""
    password = "super-secret-password-123"
    user = await create_db_user(password=password, enabled=False)

    client_ip = f"10.3.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with create_client(ip=client_ip) as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": password},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_me_endpoint_token_validation() -> None:
    """E05 with valid token returns user; with no token 401 AUTH_REQUIRED;

    with malformed/expired/wrong-issuer/wrong-audience/wrong-alg token 401 INVALID_TOKEN.
    """
    password = "super-secret-password-123"
    user = await create_db_user(password=password)

    settings = get_settings()
    jwt_secret = settings.JWT_SECRET or "test-jwt-secret-minimum-32-bytes-long-12345678"

    client_ip = f"10.4.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with create_client(ip=client_ip) as client:
        # 1. No token -> 401 AUTH_REQUIRED
        resp_no_token = await client.get("/api/v1/me")
        assert resp_no_token.status_code == 401
        assert resp_no_token.json()["error"]["code"] == "AUTH_REQUIRED"

        # 2. Invalid auth scheme -> 401 AUTH_REQUIRED
        resp_basic = await client.get("/api/v1/me", headers={"Authorization": "Basic 12345"})
        assert resp_basic.status_code == 401
        assert resp_basic.json()["error"]["code"] == "AUTH_REQUIRED"

        # 3. Valid token -> 200 User
        now = datetime.now(timezone.utc)
        valid_claims = {
            "sub": str(user.id),
            "role": user.role,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=15)).timestamp()),
            "jti": str(uuid.uuid4()),
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
        }
        valid_token = jwt.encode(valid_claims, jwt_secret, algorithm="HS256")
        resp_valid = await client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {valid_token}"}
        )
        assert resp_valid.status_code == 200
        assert resp_valid.json()["id"] == str(user.id)
        assert resp_valid.json()["email"] == user.email

        # 4. Malformed token -> 401 INVALID_TOKEN
        resp_malformed = await client.get(
            "/api/v1/me", headers={"Authorization": "Bearer not-a-real-jwt-token"}
        )
        assert resp_malformed.status_code == 401
        assert resp_malformed.json()["error"]["code"] == "INVALID_TOKEN"

        # 5. Expired token -> 401 INVALID_TOKEN
        expired_claims = dict(valid_claims)
        expired_claims["exp"] = int((now - timedelta(minutes=5)).timestamp())
        expired_claims["jti"] = str(uuid.uuid4())
        expired_token = jwt.encode(expired_claims, jwt_secret, algorithm="HS256")
        resp_expired = await client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {expired_token}"}
        )
        assert resp_expired.status_code == 401
        assert resp_expired.json()["error"]["code"] == "INVALID_TOKEN"

        # 6. Wrong issuer -> 401 INVALID_TOKEN
        bad_iss_claims = dict(valid_claims)
        bad_iss_claims["iss"] = "attacker-domain"
        bad_iss_claims["jti"] = str(uuid.uuid4())
        bad_iss_token = jwt.encode(bad_iss_claims, jwt_secret, algorithm="HS256")
        resp_bad_iss = await client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {bad_iss_token}"}
        )
        assert resp_bad_iss.status_code == 401
        assert resp_bad_iss.json()["error"]["code"] == "INVALID_TOKEN"

        # 7. Wrong audience -> 401 INVALID_TOKEN
        bad_aud_claims = dict(valid_claims)
        bad_aud_claims["aud"] = "wrong-audience"
        bad_aud_claims["jti"] = str(uuid.uuid4())
        bad_aud_token = jwt.encode(bad_aud_claims, jwt_secret, algorithm="HS256")
        resp_bad_aud = await client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {bad_aud_token}"}
        )
        assert resp_bad_aud.status_code == 401
        assert resp_bad_aud.json()["error"]["code"] == "INVALID_TOKEN"

        # 8. Wrong secret / signature -> 401 INVALID_TOKEN
        wrong_secret_token = jwt.encode(
            valid_claims, "wrong-signing-secret-key-32-chars-long", algorithm="HS256"
        )
        resp_wrong_sig = await client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {wrong_secret_token}"}
        )
        assert resp_wrong_sig.status_code == 401
        assert resp_wrong_sig.json()["error"]["code"] == "INVALID_TOKEN"


@pytest.mark.asyncio
async def test_database_changes_take_immediate_effect() -> None:
    """Role/enabled changes in the DATABASE take effect immediately even though old JWT

    claims state otherwise. Disabling user invalidates active JWT; updating role in DB
    is reflected immediately on next request.
    """
    user = await create_db_user(role="member", enabled=True)
    settings = get_settings()
    jwt_secret = settings.JWT_SECRET or "test-jwt-secret-minimum-32-bytes-long-12345678"

    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user.id),
        "role": "member",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "jti": str(uuid.uuid4()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
    }
    token = jwt.encode(claims, jwt_secret, algorithm="HS256")

    client_ip = f"10.5.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with create_client(ip=client_ip) as client:
        # Initial access succeeds
        r1 = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r1.status_code == 200
        assert r1.json()["role"] == "member"

        # Disable user in DB
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            async with session.begin():
                stmt = select(User).where(User.id == user.id)
                db_user = (await session.execute(stmt)).scalar_one()
                db_user.enabled = False

        # Existing valid JWT must now be rejected
        r2 = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 401
        assert r2.json()["error"]["code"] == "INVALID_TOKEN"

        # Re-enable user and elevate to admin in DB
        async with sessionmaker() as session:
            async with session.begin():
                stmt = select(User).where(User.id == user.id)
                db_user = (await session.execute(stmt)).scalar_one()
                db_user.enabled = True
                db_user.role = "admin"

        # SAME token (which has role="member" in JWT claims) now returns role="admin"
        r3 = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r3.status_code == 200
        assert r3.json()["role"] == "admin"

        # Verify authorize(Policy.admin) succeeds with this token because DB role is reloaded
        from starlette.requests import Request

        from app.auth.dependencies import Policy, authorize

        req = Request(
            {
                "type": "http",
                "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
            }
        )
        async with sessionmaker() as session:
            admin_scope = await authorize(Policy.admin)(req, session)
            assert admin_scope.principal_id == user.id


@pytest.mark.asyncio
async def test_rate_limiting_ip_and_email() -> None:
    """Rate limiting: exceeding 10 login attempts per IP per minute returns 429 with integer

    Retry-After; exceeding 5 per normalized email likewise.
    """
    client_ip_email_test = f"10.6.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    test_email = f"rate_limit_{uuid.uuid4().hex[:8]}@example.com"

    async with create_client(ip=client_ip_email_test) as client:
        # 5 attempts for the same email
        for _ in range(5):
            r = await client.post(
                "/api/v1/auth/login",
                json={"email": test_email, "password": "wrong-password-123"},
            )
            assert r.status_code == 401

        # 6th attempt with SAME email returns 429
        r6 = await client.post(
            "/api/v1/auth/login",
            json={"email": test_email, "password": "wrong-password-123"},
        )
        assert r6.status_code == 429
        assert r6.json()["error"]["code"] == "RATE_LIMITED"
        assert "Retry-After" in r6.headers
        assert int(r6.headers["Retry-After"]) > 0
        assert r6.json()["error"]["details"]["retry_after"] == int(r6.headers["Retry-After"])

    # Test IP rate limit: 10 attempts per IP
    client_ip_test = f"10.7.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with create_client(ip=client_ip_test) as client:
        for i in range(10):
            distinct_email = f"ip_rate_{i}_{uuid.uuid4().hex[:8]}@example.com"
            r = await client.post(
                "/api/v1/auth/login",
                json={"email": distinct_email, "password": "wrong-password-123"},
            )
            assert r.status_code == 401

        # 11th attempt from same IP returns 429
        r11 = await client.post(
            "/api/v1/auth/login",
            json={
                "email": f"ip_rate_11_{uuid.uuid4().hex[:8]}@example.com",
                "password": "wrong-password-123",
            },
        )
        assert r11.status_code == 429
        assert r11.json()["error"]["code"] == "RATE_LIMITED"
        assert int(r11.headers["Retry-After"]) > 0


@pytest.mark.asyncio
async def test_failed_login_rate_consumption_survives_domain_rollback() -> None:
    """FAILED-LOGIN RATE CONSUMPTION SURVIVES DOMAIN ROLLBACK:

    Failed authentication must still consume the rate bucket even though its domain
    transaction rolled back. Assert the persisted count increased.
    """
    test_email = f"rollback_{uuid.uuid4().hex[:8]}@example.com"
    norm_email = normalize_email(test_email)
    email_hash = hash_identity(norm_email)
    client_ip = f"10.8.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"

    sessionmaker = get_sessionmaker()

    # Query before
    async with sessionmaker() as session:
        res = await session.execute(
            select(RateLimit.count).where(
                RateLimit.scope == "login:email",
                RateLimit.identity_hash == email_hash,
            )
        )
        initial_count = res.scalar_one_or_none() or 0

    # Perform failed login
    async with create_client(ip=client_ip) as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": test_email, "password": "wrong-password-123"},
        )
        assert resp.status_code == 401

    # Query after in a fresh session: count MUST have increased despite domain rollback!
    async with sessionmaker() as session:
        res = await session.execute(
            select(RateLimit.count).where(
                RateLimit.scope == "login:email",
                RateLimit.identity_hash == email_hash,
            )
        )
        final_count = res.scalar_one()

    assert final_count == initial_count + 1


@pytest.mark.asyncio
async def test_request_id_middleware() -> None:
    """Request-ID middleware: a non-UUID inbound X-Request-ID is replaced by a valid UUID;

    a valid inbound UUID is preserved; every response carries X-Request-ID.
    """
    async with create_client() as client:
        # 1. Non-UUID inbound ID on API endpoint
        resp_invalid = await client.get("/api/v1/me", headers={"X-Request-ID": "not-a-valid-uuid"})
        out_id = resp_invalid.headers.get("X-Request-ID")
        assert out_id is not None
        assert out_id != "not-a-valid-uuid"
        parsed_uuid = uuid.UUID(out_id)
        assert str(parsed_uuid) == out_id

        # 2. Valid inbound UUID is preserved
        inbound_valid = "550e8400-e29b-41d4-a716-446655440000"
        resp_valid = await client.get("/api/v1/me", headers={"X-Request-ID": inbound_valid})
        assert resp_valid.headers.get("X-Request-ID") == inbound_valid

        # 3. Absent X-Request-ID gets freshly generated UUID
        resp_none = await client.get("/api/v1/me")
        fresh_id = resp_none.headers.get("X-Request-ID")
        assert fresh_id is not None
        assert str(uuid.UUID(fresh_id)) == fresh_id

        # 4. JSON body must NOT contain request ID
        assert "request_id" not in resp_none.json()
        assert "X-Request-ID" not in resp_none.json()
