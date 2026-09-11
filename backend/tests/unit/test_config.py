import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from starlette.requests import Request

from app.auth.dependencies import Policy, authorize
from app.auth.models import User
from app.auth.passwords import hash_password
from app.auth.rate_limit import hash_identity
from app.auth.service import login
from app.config import Settings

ERR_JWT_CLOSED = "JWT_SECRET must be configured and at least 32 bytes"
ERR_HMAC_CLOSED = "RATE_LIMIT_HMAC_SECRET must be configured and at least 32 bytes"


def test_settings_rejects_too_short_jwt_secret_in_local() -> None:
    """Settings must reject JWT_SECRET shorter than 32 bytes in non-production environments."""
    with pytest.raises(ValueError, match="JWT_SECRET must be at least 32 bytes"):
        Settings(
            APP_ENV="local",
            JWT_SECRET="too-short-key",
            RATE_LIMIT_HMAC_SECRET="valid-rate-limit-secret-with-at-least-32-bytes",
            _env_file=None,
        )


def test_settings_rejects_too_short_rate_limit_hmac_secret_in_local() -> None:
    """Settings must reject RATE_LIMIT_HMAC_SECRET shorter than 32 bytes in non-production."""
    with pytest.raises(ValueError, match="RATE_LIMIT_HMAC_SECRET must be at least 32 bytes"):
        Settings(
            APP_ENV="local",
            JWT_SECRET="valid-jwt-secret-with-at-least-32-bytes-long",
            RATE_LIMIT_HMAC_SECRET="too-short-hmac",
            _env_file=None,
        )


def test_settings_production_requires_both_secrets_and_min_length() -> None:
    """Settings in production must reject missing or too-short secrets."""
    with pytest.raises(ValueError, match="JWT_SECRET must be at least 32 bytes in production"):
        Settings(
            APP_ENV="production",
            JWT_SECRET="",
            RATE_LIMIT_HMAC_SECRET="valid-rate-limit-secret-with-at-least-32-bytes",
            _env_file=None,
        )

    with pytest.raises(
        ValueError, match="RATE_LIMIT_HMAC_SECRET must be at least 32 bytes in production"
    ):
        Settings(
            APP_ENV="production",
            JWT_SECRET="valid-jwt-secret-with-at-least-32-bytes-long",
            RATE_LIMIT_HMAC_SECRET="",
            _env_file=None,
        )

    with pytest.raises(
        ValueError, match="RATE_LIMIT_HMAC_SECRET must be at least 32 bytes in production"
    ):
        Settings(
            APP_ENV="production",
            JWT_SECRET="valid-jwt-secret-with-at-least-32-bytes-long",
            RATE_LIMIT_HMAC_SECRET="too-short-hmac",
            _env_file=None,
        )


def test_settings_production_requires_metrics_token_and_sentry_dsn() -> None:
    """Production settings must reject missing METRICS_TOKEN and missing SENTRY_DSN."""
    valid_key = "valid-secret-minimum-32-bytes-long-12345"

    with pytest.raises(ValueError, match="METRICS_TOKEN must be at least 32 bytes in production"):
        Settings(
            APP_ENV="production",
            JWT_SECRET=valid_key,
            RATE_LIMIT_HMAC_SECRET=valid_key,
            METRICS_TOKEN="",
            SENTRY_DSN="https://example@sentry.invalid/1",
            _env_file=None,
        )

    with pytest.raises(ValueError, match="METRICS_TOKEN must be at least 32 bytes in production"):
        Settings(
            APP_ENV="production",
            JWT_SECRET=valid_key,
            RATE_LIMIT_HMAC_SECRET=valid_key,
            METRICS_TOKEN="too-short",
            SENTRY_DSN="https://example@sentry.invalid/1",
            _env_file=None,
        )

    with pytest.raises(ValueError, match="SENTRY_DSN is required in production"):
        Settings(
            APP_ENV="production",
            JWT_SECRET=valid_key,
            RATE_LIMIT_HMAC_SECRET=valid_key,
            METRICS_TOKEN=valid_key,
            SENTRY_DSN="",
            _env_file=None,
        )

    # Valid production settings instantiate without error
    prod = Settings(
        APP_ENV="production",
        JWT_SECRET=valid_key,
        RATE_LIMIT_HMAC_SECRET=valid_key,
        METRICS_TOKEN=valid_key,
        SENTRY_DSN="https://example@sentry.invalid/1",
        _env_file=None,
    )
    assert prod.APP_ENV == "production"
    assert prod.SENTRY_DSN == "https://example@sentry.invalid/1"


def test_rate_limit_fails_closed_when_secret_unset_in_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hash_identity must fail closed (raise RuntimeError) when secret is unset in local."""
    test_settings = Settings(
        APP_ENV="local",
        JWT_SECRET="valid-jwt-secret-with-at-least-32-bytes-long",
        RATE_LIMIT_HMAC_SECRET="",
        _env_file=None,
    )
    monkeypatch.setattr("app.auth.rate_limit.get_settings", lambda: test_settings)

    with pytest.raises(RuntimeError, match=ERR_HMAC_CLOSED):
        hash_identity("user@example.com")


def test_rate_limit_fails_closed_when_secret_too_short_in_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hash_identity must fail closed (raise RuntimeError) when secret is shorter than 32 bytes."""
    test_settings = Settings.model_construct(
        APP_ENV="local",
        RATE_LIMIT_HMAC_SECRET="short-secret",
    )
    monkeypatch.setattr("app.auth.rate_limit.get_settings", lambda: test_settings)

    with pytest.raises(RuntimeError, match=ERR_HMAC_CLOSED):
        hash_identity("user@example.com")


@pytest.mark.asyncio
async def test_auth_dependency_fails_closed_when_jwt_secret_unset_in_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth dependency must fail closed (raise RuntimeError) when JWT_SECRET is unset in local."""
    test_settings = Settings(
        APP_ENV="local",
        JWT_SECRET="",
        RATE_LIMIT_HMAC_SECRET="valid-rate-limit-secret-with-at-least-32-bytes",
        _env_file=None,
    )
    monkeypatch.setattr("app.auth.dependencies.get_settings", lambda: test_settings)

    # Valid signed token with some key
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": "member",
            "iat": 1000,
            "exp": 9999999999,
            "jti": str(uuid.uuid4()),
            "iss": "commonsbook",
            "aud": "commonsbook-web",
        },
        "any-random-key-at-least-32-bytes-long!",
        algorithm="HS256",
    )

    scope_dep = authorize(Policy.authenticated)
    fake_scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/test",
        "headers": [(b"authorization", f"Bearer {token}".encode("utf-8"))],
    }
    req = Request(fake_scope)

    with pytest.raises(RuntimeError, match=ERR_JWT_CLOSED):
        await scope_dep(req, session=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_auth_dependency_fails_closed_when_jwt_secret_too_short_in_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth dependency must fail closed when JWT_SECRET is shorter than 32 bytes."""
    test_settings = Settings.model_construct(
        APP_ENV="local",
        JWT_SECRET="short-jwt-secret",
    )
    monkeypatch.setattr("app.auth.dependencies.get_settings", lambda: test_settings)

    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": "member",
            "iat": 1000,
            "exp": 9999999999,
            "jti": str(uuid.uuid4()),
            "iss": "commonsbook",
            "aud": "commonsbook-web",
        },
        "short-jwt-secret",
        algorithm="HS256",
    )

    scope_dep = authorize(Policy.authenticated)
    fake_scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/test",
        "headers": [(b"authorization", f"Bearer {token}".encode("utf-8"))],
    }
    req = Request(fake_scope)

    with pytest.raises(RuntimeError, match=ERR_JWT_CLOSED):
        await scope_dep(req, session=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_auth_service_fails_closed_when_jwt_secret_unset_in_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth service login must fail closed when JWT_SECRET is unset in local."""
    test_settings = Settings(
        APP_ENV="local",
        JWT_SECRET="",
        RATE_LIMIT_HMAC_SECRET="valid-rate-limit-secret-with-at-least-32-bytes",
        _env_file=None,
    )
    monkeypatch.setattr("app.auth.service.get_settings", lambda: test_settings)

    mock_session = AsyncMock()
    mock_user = MagicMock(spec=User)
    mock_user.id = uuid.uuid4()
    mock_user.email = "test@example.com"
    mock_user.display_name = "Test User"
    mock_user.role = "member"
    mock_user.enabled = True
    mock_user.created_at = datetime.now(timezone.utc)
    mock_user.updated_at = datetime.now(timezone.utc)
    mock_user.password_hash = hash_password("ValidPassword123!")

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user
    mock_session.execute.return_value = mock_result

    with pytest.raises(RuntimeError, match=ERR_JWT_CLOSED):
        await login(
            session=mock_session,
            email="test@example.com",
            password="ValidPassword123!",
            now=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_auth_service_fails_closed_when_jwt_secret_too_short_in_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth service login must fail closed when JWT_SECRET is shorter than 32 bytes in local."""
    test_settings = Settings.model_construct(
        APP_ENV="local",
        JWT_SECRET="short-jwt-secret",
    )
    monkeypatch.setattr("app.auth.service.get_settings", lambda: test_settings)

    mock_session = AsyncMock()
    mock_user = MagicMock(spec=User)
    mock_user.id = uuid.uuid4()
    mock_user.email = "test@example.com"
    mock_user.display_name = "Test User"
    mock_user.role = "member"
    mock_user.enabled = True
    mock_user.created_at = datetime.now(timezone.utc)
    mock_user.updated_at = datetime.now(timezone.utc)
    mock_user.password_hash = hash_password("ValidPassword123!")

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user
    mock_session.execute.return_value = mock_result

    with pytest.raises(RuntimeError, match=ERR_JWT_CLOSED):
        await login(
            session=mock_session,
            email="test@example.com",
            password="ValidPassword123!",
            now=datetime.now(timezone.utc),
        )
