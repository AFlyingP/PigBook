import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import DBAPIError

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

from app.admin.audit import append_audit_log
from app.admin.models import AuditLog
from app.auth.models import User
from app.auth.passwords import hash_password
from app.bookings.models import Booking
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app
from app.notifications.models import NotificationDelivery, Outbox
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
    display_name: str = "Test Operations User",
    role: str = "admin",
    enabled: bool = True,
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


async def create_fixture_booking(user: User) -> Booking:
    now = datetime.now(timezone.utc)
    res_id = uuid.uuid4()
    booking_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            r = Resource(
                id=res_id,
                name=f"Res_{uuid.uuid4().hex[:6]}",
                description="desc",
                location="Loc",
                active=True,
                version=1,
            )
            session.add(r)
            await session.flush()
            b = Booking(
                id=booking_id,
                resource_id=res_id,
                user_id=user.id,
                created_by=user.id,
                kind="reservation",
                time_range=Range(
                    now + timedelta(days=1), now + timedelta(days=1, hours=1), bounds="[)"
                ),
                status="confirmed",
                version=1,
            )
            session.add(b)
            await session.flush()
    return b


@pytest.mark.asyncio
async def test_admin_audit_listing_and_redaction() -> None:
    """E30: GET /api/v1/admin/audit pagination, target_id filter, and redaction."""
    admin = await create_user(role="admin", enabled=True)
    admin_token = make_token(admin)
    target_user = await create_user(role="member", enabled=True)
    now = datetime.now(timezone.utc)

    # Append test audit records
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            await append_audit_log(
                session,
                action="admin.user_update",
                target_type="user",
                target_id=target_user.id,
                actor_id=admin.id,
                request_id=uuid.uuid4(),
                details={
                    "fields": ["role", "enabled"],
                    "old_role": "member",
                    "new_role": "admin",
                    "sensitive_secret": "SHOULD_BE_FILTERED",
                },
                now=now - timedelta(seconds=10),
            )
            await append_audit_log(
                session,
                action="admin.resource_create",
                target_type="resource",
                target_id=uuid.uuid4(),
                actor_id=admin.id,
                request_id=uuid.uuid4(),
                details={"fields": ["name", "location"]},
                now=now,
            )

    client_ip = f"10.7.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        # All audit entries
        r = await client.get(
            "/api/v1/admin/audit?limit=10&offset=0",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert data["total"] >= 2
        # Ordering: created_at descending
        timestamps = [item["created_at"] for item in data["items"]]
        assert timestamps == sorted(timestamps, reverse=True)

        # Target ID filter
        r_filtered = await client.get(
            f"/api/v1/admin/audit?target_id={target_user.id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_filtered.status_code == 200
        filtered_items = r_filtered.json()["items"]
        assert len(filtered_items) >= 1
        for item in filtered_items:
            assert item["target_id"] == str(target_user.id)
            # Verify details are redacted (no secrets leaked)
            assert "sensitive_secret" not in item["details"]
            assert item["details"]["fields"] == ["role", "enabled"]
            assert item["details"]["old_role"] == "member"
            assert item["details"]["new_role"] == "admin"


@pytest.mark.asyncio
async def test_admin_outbox_listing_and_error_redaction() -> None:
    """E31: GET /api/v1/admin/outbox pagination, status filter, and error category redaction."""
    admin = await create_user(role="admin", enabled=True)
    admin_token = make_token(admin)
    booking = await create_fixture_booking(admin)
    now = datetime.now(timezone.utc)

    sessionmaker = get_sessionmaker()
    dead_id = uuid.uuid4()
    pending_id = uuid.uuid4()
    async with sessionmaker() as session:
        async with session.begin():
            # Dead event with sensitive error text
            e_dead = Outbox(
                id=dead_id,
                event_type="booking_confirmed",
                aggregate_id=booking.id,
                aggregate_version=1,
                payload={"schema_version": 1, "booking_id": str(booking.id)},
                status="dead",
                attempts=8,
                occurred_at=now - timedelta(minutes=5),
                available_at=now,
                last_error="smtp_auth_failure: Password 'super_secret' rejected by smtp.host",
            )
            # Pending event
            e_pending = Outbox(
                id=pending_id,
                event_type="booking_cancelled",
                aggregate_id=booking.id,
                aggregate_version=2,
                payload={"schema_version": 1, "booking_id": str(booking.id)},
                status="pending",
                attempts=0,
                occurred_at=now,
                available_at=now,
                last_error=None,
            )
            session.add_all([e_dead, e_pending])

    client_ip = f"10.7.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        # All events
        r = await client.get(
            "/api/v1/admin/outbox",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 2

        # Status filter: dead
        r_dead = await client.get(
            "/api/v1/admin/outbox?status=dead",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_dead.status_code == 200
        dead_items = r_dead.json()["items"]
        assert len(dead_items) >= 1
        target_dead = next(item for item in dead_items if item["id"] == str(dead_id))
        assert target_dead["status"] == "dead"
        assert target_dead["attempts"] == 8
        # Error must be redacted category only, without password
        assert "super_secret" not in (target_dead["last_error"] or "")
        assert target_dead["last_error"] == "smtp_auth_failure"

        # Invalid status filter -> 422 VALIDATION_ERROR (R1 HTTP contract)
        r_bogus = await client.get(
            "/api/v1/admin/outbox?status=bogus",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_bogus.status_code == 422
        assert r_bogus.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_admin_outbox_retry_dead_only_and_repeat_rejection() -> None:
    """E32: POST /api/v1/admin/outbox/{id}/retry transitions dead -> pending,

    appends audit, and rejects retry on non-dead state (409 INVALID_STATE).
    """
    admin = await create_user(role="admin", enabled=True)
    admin_token = make_token(admin)
    booking = await create_fixture_booking(admin)
    now = datetime.now(timezone.utc)

    sessionmaker = get_sessionmaker()
    dead_id = uuid.uuid4()
    delivered_id = uuid.uuid4()
    async with sessionmaker() as session:
        async with session.begin():
            e_dead = Outbox(
                id=dead_id,
                event_type="booking_confirmed",
                aggregate_id=booking.id,
                aggregate_version=1,
                payload={"schema_version": 1, "booking_id": str(booking.id)},
                status="dead",
                attempts=8,
                occurred_at=now - timedelta(minutes=10),
                available_at=now - timedelta(minutes=5),
                last_error="connection_timeout",
            )
            e_delivered = Outbox(
                id=delivered_id,
                event_type="booking_confirmed",
                aggregate_id=booking.id,
                aggregate_version=2,
                payload={"schema_version": 1, "booking_id": str(booking.id)},
                status="delivered",
                attempts=1,
                occurred_at=now,
                available_at=now,
                delivered_at=now,
            )
            session.add_all([e_dead, e_delivered])

    client_ip = f"10.7.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        # 1. Retrying non-dead event -> 409 INVALID_STATE
        r_inv = await client.post(
            f"/api/v1/admin/outbox/{delivered_id}/retry",
            json={},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_inv.status_code == 409
        assert r_inv.json()["error"]["code"] == "INVALID_STATE"

        # 2. Retrying dead event -> 200 OK
        r_retry = await client.post(
            f"/api/v1/admin/outbox/{dead_id}/retry",
            json={},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_retry.status_code == 200
        view = r_retry.json()
        assert view["id"] == str(dead_id)
        assert view["status"] == "pending"
        assert view["attempts"] == 0
        assert view["last_error"] is None

        # Verify DB state
        async with sessionmaker() as session:
            stmt = select(Outbox).where(Outbox.id == dead_id)
            updated_e = (await session.execute(stmt)).scalar_one()
            assert updated_e.status == "pending"
            assert updated_e.attempts == 0
            assert updated_e.last_error is None

            # Verify audit entry appended
            audit_stmt = select(AuditLog).where(
                AuditLog.action == "admin.outbox_retry",
                AuditLog.target_id == dead_id,
            )
            audit_row = (await session.execute(audit_stmt)).scalar_one_or_none()
            assert audit_row is not None
            assert audit_row.details["previous_status"] == "dead"
            assert audit_row.details["attempts"] == 0

        # 3. Repeat retry: immediate second retry fails with 409 INVALID_STATE (now pending)
        r_repeat = await client.post(
            f"/api/v1/admin/outbox/{dead_id}/retry",
            json={},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_repeat.status_code == 409
        assert r_repeat.json()["error"]["code"] == "INVALID_STATE"


@pytest.mark.asyncio
async def test_admin_outbox_retry_preserves_delivery_receipt() -> None:
    """E32: Retrying an event retains any existing delivery receipts in notification_deliveries

    so already-sent notifications cannot be re-dispatched.
    """
    admin = await create_user(role="admin", enabled=True)
    admin_token = make_token(admin)
    booking = await create_fixture_booking(admin)
    now = datetime.now(timezone.utc)

    event_id = uuid.uuid4()
    delivery_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            e = Outbox(
                id=event_id,
                event_type="booking_confirmed",
                aggregate_id=booking.id,
                aggregate_version=1,
                payload={"schema_version": 1, "booking_id": str(booking.id)},
                status="dead",
                attempts=8,
                occurred_at=now,
                available_at=now,
                last_error="smtp_error",
            )
            session.add(e)
            await session.flush()
            # An existing delivery receipt with status 'sent'
            d = NotificationDelivery(
                id=delivery_id,
                event_id=event_id,
                recipient_id=admin.id,
                channel="email",
                state="sent",
                provider_message_id="msg-12345",
                sent_at=now,
            )
            session.add(d)

    client_ip = f"10.7.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    async with make_client(ip=client_ip) as client:
        r = await client.post(
            f"/api/v1/admin/outbox/{event_id}/retry",
            json={},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200

        # Verify receipt is intact and unaffected
        async with sessionmaker() as session:
            stmt = select(NotificationDelivery).where(NotificationDelivery.id == delivery_id)
            rec = (await session.execute(stmt)).scalar_one_or_none()
            assert rec is not None
            assert rec.state == "sent"
            assert rec.provider_message_id == "msg-12345"


@pytest.mark.asyncio
async def test_audit_log_immutability_at_runtime() -> None:
    """Spec 3.3, 8.3: Runtime database role has SELECT and INSERT only on audit_log.

    UPDATE and DELETE statements must be rejected by PostgreSQL permissions.
    """
    sessionmaker = get_sessionmaker()
    log_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # INSERT is allowed
    async with sessionmaker() as session:
        async with session.begin():
            entry = AuditLog(
                id=log_id,
                action="test.immutable",
                target_type="system",
                request_id=uuid.uuid4(),
                details={"test": True},
                created_at=now,
            )
            session.add(entry)

    role_name = f"runtime_test_{uuid.uuid4().hex[:8]}"
    # Provision runtime role matching Spec 3.3 (SELECT / INSERT only on audit_log)
    async with sessionmaker() as session:
        async with session.begin():
            await session.execute(text(f"CREATE ROLE {role_name} NOINHERIT"))
            await session.execute(text(f"GRANT USAGE ON SCHEMA public TO {role_name}"))
            await session.execute(text(f"GRANT SELECT, INSERT ON audit_log TO {role_name}"))

    try:
        # UPDATE must fail under runtime role
        async with sessionmaker() as session:
            with pytest.raises((DBAPIError, Exception)) as exc_info:
                async with session.begin():
                    await session.execute(text(f"SET ROLE {role_name}"))
                    await session.execute(
                        text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"),
                        {"id": log_id},
                    )
            err_msg = str(exc_info.value).lower()
            assert "permission denied" in err_msg or "privilege" in err_msg

        # DELETE must fail under runtime role
        async with sessionmaker() as session:
            with pytest.raises((DBAPIError, Exception)) as exc_info:
                async with session.begin():
                    await session.execute(text(f"SET ROLE {role_name}"))
                    await session.execute(
                        text("DELETE FROM audit_log WHERE id = :id"),
                        {"id": log_id},
                    )
            err_msg = str(exc_info.value).lower()
            assert "permission denied" in err_msg or "privilege" in err_msg
    finally:
        async with sessionmaker() as session:
            async with session.begin():
                await session.execute(text(f"DROP OWNED BY {role_name}"))
                await session.execute(text(f"DROP ROLE IF EXISTS {role_name}"))
