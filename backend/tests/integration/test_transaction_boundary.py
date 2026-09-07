import base64
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi.routing import APIRoute, _IncludedRouter
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.session import SessionTransaction

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.auth.models import Invitation, User
from app.auth.passwords import hash_password
from app.config import get_settings
from app.db.session import get_sessionmaker, transaction_dependency
from app.main import app

get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _cleanup_engine() -> None:
    import app.db.session

    if app.db.session._engine is not None:
        await app.db.session._engine.dispose()
        app.db.session._engine = None
        app.db.session._sessionmaker = None
        app.db.session._engine_url = None
    yield
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


async def create_invitation_in_db(
    *,
    email: str,
    role: str = "member",
    creator_id: uuid.UUID,
    now: datetime,
) -> tuple[str, Invitation]:
    raw_bytes = os.urandom(32)
    raw_token = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    inv = Invitation(
        id=uuid.uuid4(),
        email=email,
        token_hash=token_hash,
        role=role,
        created_by=creator_id,
        created_at=now,
        expires_at=now + timedelta(days=7),
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(inv)
    return raw_token, inv


def make_client(ip: str = "127.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False, client=(ip, 12345)),
        base_url="http://localhost:5173",
    )


@pytest.mark.asyncio
async def test_commit_failure_prevents_successful_response() -> None:
    admin = await create_test_user(role="admin")
    now = datetime.now(timezone.utc)
    email = f"reg_fail_{uuid.uuid4().hex[:8]}@example.com"
    raw_token, _ = await create_invitation_in_db(
        email=email, role="member", creator_id=admin.id, now=now
    )

    original_commit = SessionTransaction.commit

    flushed_sessions: set[int] = set()

    def fail_unit_of_work_commit(self: SessionTransaction) -> None:
        if self._parent is not None:
            flushed_sessions.add(id(self.session))
        elif id(self.session) in flushed_sessions:
            raise OperationalError(
                "simulated commit failure",
                {},
                Exception("commit serialization failure"),
            )
        original_commit(self)

    client_ip = f"10.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    with patch.object(SessionTransaction, "commit", new=fail_unit_of_work_commit):
        async with make_client(ip=client_ip) as client:
            response = await client.post(
                "/api/v1/auth/register",
                json={
                    "invitation_token": raw_token,
                    "email": email,
                    "password": "strong-password-123",
                    "display_name": "Rollback Test User",
                },
            )

    # Client must not receive a 201 Created on commit failure
    assert response.status_code >= 500, (
        f"Expected 5xx error response, got {response.status_code}: {response.text}"
    )

    # Verify that the user row was not persisted
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        res = await session.execute(select(User).where(User.email == email))
        assert res.scalar_one_or_none() is None


def test_routes_use_function_scoped_transaction_dependency() -> None:
    def iter_api_routes(app_or_router):
        for route in getattr(app_or_router, "routes", []):
            if isinstance(route, APIRoute):
                yield route
            elif isinstance(route, _IncludedRouter):
                yield from iter_api_routes(route.original_router)
            elif hasattr(route, "routes"):
                yield from iter_api_routes(route)

    transaction_routes: list[tuple[str, str | None]] = []
    for route in iter_api_routes(app):
        for dep in route.dependant.dependencies:
            if dep.call == transaction_dependency:
                transaction_routes.append((route.path, dep.scope))

    assert len(transaction_routes) >= 5, (
        f"Expected at least 5 routes, found {len(transaction_routes)}"
    )
    for path, scope in transaction_routes:
        assert scope == "function", (
            f"Route {path} injects transaction_dependency with scope={scope!r}, expected 'function'"
        )
