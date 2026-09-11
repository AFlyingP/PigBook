import os

import httpx
import pytest
from sqlalchemy import text

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.db.compatibility import (
    SchemaIncompatibleError,
    load_supported_revisions,
    validate_schema_compatibility,
)
from app.db.session import get_sessionmaker
from app.main import app


@pytest.fixture(autouse=True)
async def _cleanup_engine() -> None:
    yield
    import app.db.session

    if app.db.session._engine is not None:
        await app.db.session._engine.dispose()
        app.db.session._engine = None
        app.db.session._sessionmaker = None
        app.db.session._engine_url = None


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:5173",
    )


@pytest.mark.asyncio
async def test_supported_revisions_file_content() -> None:
    """schema_compatibility.json contains a nonempty list initially with 0004_operations."""
    revisions = load_supported_revisions()
    assert isinstance(revisions, list)
    assert len(revisions) >= 1
    assert "0004_operations" in revisions


@pytest.mark.asyncio
async def test_schema_compatibility_one_allowed_head_is_ready() -> None:
    """When the live DB contains exactly one allowed head, readyz returns 200 ready."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        head = await validate_schema_compatibility(session)
        assert head == "0004_operations"

    async with make_client() as client:
        r = await client.get("/readyz")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert "version" in data


@pytest.mark.asyncio
async def test_schema_compatibility_unknown_head_returns_503() -> None:
    """Unknown revision head causes validation failure and readyz returns 503."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            await session.execute(text("UPDATE alembic_version SET version_num = '9999_unknown'"))

    try:
        async with sessionmaker() as session:
            with pytest.raises(SchemaIncompatibleError, match="Unsupported Alembic revision head"):
                await validate_schema_compatibility(session)

        async with make_client() as client:
            r = await client.get("/readyz")
            assert r.status_code == 503
            assert r.headers.get("retry-after") == "1"
            err = r.json()["error"]
            assert err["code"] == "RETRYABLE_UNAVAILABLE"
            assert err["message"] == "Service unavailable"
            # Database internals must not leak
            assert "9999_unknown" not in str(err)
            assert "alembic" not in str(err).lower()
    finally:
        async with sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text("UPDATE alembic_version SET version_num = '0004_operations'")
                )


@pytest.mark.asyncio
async def test_schema_compatibility_multiple_heads_returns_503() -> None:
    """Multiple revision heads cause validation failure and readyz returns 503."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            await session.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('0005_other')")
            )

    try:
        async with sessionmaker() as session:
            with pytest.raises(SchemaIncompatibleError, match="Multiple Alembic revision heads"):
                await validate_schema_compatibility(session)

        async with make_client() as client:
            r = await client.get("/readyz")
            assert r.status_code == 503
            assert r.headers.get("retry-after") == "1"
            err = r.json()["error"]
            assert err["code"] == "RETRYABLE_UNAVAILABLE"
            assert err["message"] == "Service unavailable"
    finally:
        async with sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text("DELETE FROM alembic_version WHERE version_num = '0005_other'")
                )


@pytest.mark.asyncio
async def test_schema_compatibility_missing_head_returns_503() -> None:
    """When alembic_version is empty, validation raises and readyz returns 503."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM alembic_version"))

    try:
        async with sessionmaker() as session:
            with pytest.raises(SchemaIncompatibleError, match="Missing Alembic revision head"):
                await validate_schema_compatibility(session)

        async with make_client() as client:
            r = await client.get("/readyz")
            assert r.status_code == 503
            assert r.headers.get("retry-after") == "1"
            err = r.json()["error"]
            assert err["code"] == "RETRYABLE_UNAVAILABLE"
            assert err["message"] == "Service unavailable"
    finally:
        async with sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES ('0004_operations')")
                )


@pytest.mark.asyncio
async def test_schema_compatibility_no_lexical_comparison() -> None:
    """Validation uses exact allowlist membership, never min/max or lexical ordering."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # 0003_delivery is numerically and lexically smaller than 0004_operations,
        # but is NOT in the allowlist and must be rejected.
        async with session.begin():
            await session.execute(text("UPDATE alembic_version SET version_num = '0003_delivery'"))

    try:
        async with sessionmaker() as session:
            with pytest.raises(SchemaIncompatibleError):
                await validate_schema_compatibility(session)
    finally:
        async with sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    text("UPDATE alembic_version SET version_num = '0004_operations'")
                )
