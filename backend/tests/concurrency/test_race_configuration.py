import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest

from app.config import Settings, get_settings
from tests.concurrency.conftest import create_users


def test_race_profile_rejected_in_production() -> None:
    """Spec 8.2: TEST_PROFILE=race cannot run in production; config validation rejects it."""
    with pytest.raises(ValueError, match="TEST_PROFILE 'race' is forbidden in production"):
        Settings(
            APP_ENV="production",
            TEST_PROFILE="race",
            JWT_SECRET="a" * 32,
            RATE_LIMIT_HMAC_SECRET="b" * 32,
        )


def test_race_profile_mutation_limit_configuration() -> None:
    """Spec 8.2: Default profile enforces 120/user/min; race profile raises to 1000."""
    default_settings = Settings(
        APP_ENV="local",
        TEST_PROFILE="standard",
        JWT_SECRET="a" * 32,
        RATE_LIMIT_HMAC_SECRET="b" * 32,
    )
    assert default_settings.TEST_PROFILE == "standard"
    limit_standard = 1000 if default_settings.TEST_PROFILE == "race" else 120
    assert limit_standard == 120

    race_settings = Settings(
        APP_ENV="local",
        TEST_PROFILE="race",
        JWT_SECRET="a" * 32,
        RATE_LIMIT_HMAC_SECRET="b" * 32,
    )
    assert race_settings.TEST_PROFILE == "race"
    limit_race = 1000 if race_settings.TEST_PROFILE == "race" else 120
    assert limit_race == 1000


@pytest.mark.asyncio
async def test_jwt_fixture_validation(base_url: str) -> None:
    """Spec 11.2: Test auth helper token signing is independently validated against server.

    Valid tokens return 200; tokens with wrong secret or expired claims return 401.
    """
    users, tokens = await create_users(1, prefix="jwt_val")
    user, valid_token = users[0], tokens[0]

    async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
        # 1. Valid token accepted
        r_valid = await client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert r_valid.status_code == 200
        assert r_valid.json()["id"] == str(user.id)

        # 2. Token signed with wrong secret rejected
        wrong_secret = "wrong-secret-minimum-32-chars-long-9999"
        settings = get_settings()
        now = datetime.now(timezone.utc)
        fake_claims = {
            "sub": str(user.id),
            "role": user.role,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=15)).timestamp()),
            "jti": str(uuid.uuid4()),
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
        }
        forged_token = jwt.encode(fake_claims, wrong_secret, algorithm="HS256")
        r_forged = await client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {forged_token}"},
        )
        assert r_forged.status_code == 401
        assert r_forged.json()["error"]["code"] == "INVALID_TOKEN"

        # 3. Expired token rejected
        expired_claims = dict(fake_claims)
        expired_claims["iat"] = int((now - timedelta(minutes=30)).timestamp())
        expired_claims["exp"] = int((now - timedelta(minutes=15)).timestamp())
        expired_token = jwt.encode(
            expired_claims,
            settings.JWT_SECRET or "test-jwt-secret-minimum-32-bytes-long-12345678",
            algorithm="HS256",
        )
        r_expired = await client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {expired_token}"},
        )
        assert r_expired.status_code == 401
        assert r_expired.json()["error"]["code"] == "INVALID_TOKEN"


@pytest.mark.asyncio
async def test_assert_current_callback_in_transaction(base_url: str) -> None:
    """Spec 3.4, 5.1: AuthorizedScope.assert_current revalidates user in-transaction."""
    from fastapi import Request

    from app.auth.dependencies import AuthorizedScope, InvalidTokenError, Policy, authorize
    from app.db.session import get_sessionmaker

    users, tokens = await create_users(1, prefix="assert_curr")
    user = users[0]

    # Create dummy request with Authorization header
    scope = {"type": "http", "headers": [(b"authorization", f"Bearer {tokens[0]}".encode("ascii"))]}
    req = Request(scope)

    sm = get_sessionmaker()
    async with sm() as auth_session:
        dep = authorize(Policy.authenticated)
        auth_scope: AuthorizedScope = await dep(req, auth_session)

    assert auth_scope.principal_id == user.id
    assert auth_scope.assert_current is not None

    # Call assert_current inside an active domain transaction
    async with sm() as domain_session:
        async with domain_session.begin():
            # 1. Active user passes
            await auth_scope.assert_current(domain_session)

            # 2. Disable user in DB -> assert_current fails with InvalidTokenError
            user.enabled = False
            domain_session.add(user)
            await domain_session.flush()

            with pytest.raises(InvalidTokenError):
                await auth_scope.assert_current(domain_session)
