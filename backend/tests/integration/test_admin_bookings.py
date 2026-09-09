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
from app.auth.models import User
from app.auth.passwords import hash_password
from app.bookings.models import Booking
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app
from app.notifications.models import Outbox
from app.resources.models import Resource
from app.waitlist.models import WaitlistEntry

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


async def create_resource(
    *,
    name: str | None = None,
    active: bool = True,
    version: int = 1,
) -> Resource:
    if name is None:
        name = f"Resource_{uuid.uuid4().hex[:8]}"
    r = Resource(
        id=uuid.uuid4(),
        name=name,
        description="Room for admin booking tests",
        location="Floor 4",
        active=active,
        version=version,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(r)
    return r


def make_token(user: User, custom_role: str | None = None) -> str:
    settings = get_settings()
    jwt_secret = settings.JWT_SECRET or "test-jwt-secret-minimum-32-bytes-long-12345678"
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user.id),
        "role": custom_role if custom_role is not None else user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "jti": str(uuid.uuid4()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
    }
    return jwt.encode(claims, jwt_secret, algorithm="HS256")


def make_client() -> httpx.AsyncClient:
    client_ip = f"10.9.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(client_ip, 12345)),
        base_url="http://localhost:5173",
    )


@pytest.mark.asyncio
async def test_e24_list_admin_bookings_filters_and_validation() -> None:
    admin = await create_user(role="admin")
    member1 = await create_user(role="member")
    member2 = await create_user(role="member")
    resource1 = await create_resource()
    resource2 = await create_resource()

    base_time = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(
        days=2
    )

    # 1 reservation for member1 on resource1
    b1 = Booking(
        id=uuid.uuid4(),
        resource_id=resource1.id,
        user_id=member1.id,
        created_by=member1.id,
        kind="reservation",
        time_range=Range(base_time, base_time + timedelta(hours=1), bounds="[)"),
        status="confirmed",
        version=1,
    )
    # 1 reservation for member2 on resource2
    b2 = Booking(
        id=uuid.uuid4(),
        resource_id=resource2.id,
        user_id=member2.id,
        created_by=member2.id,
        kind="reservation",
        time_range=Range(
            base_time + timedelta(hours=2), base_time + timedelta(hours=3), bounds="[)"
        ),
        status="confirmed",
        version=1,
    )
    # 1 blackout on resource1 (must NEVER appear in E24)
    blackout = Booking(
        id=uuid.uuid4(),
        resource_id=resource1.id,
        user_id=None,
        created_by=admin.id,
        kind="blackout",
        time_range=Range(
            base_time + timedelta(hours=4), base_time + timedelta(hours=5), bounds="[)"
        ),
        status="confirmed",
        version=1,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(b1)
            session.add(b2)
            session.add(blackout)

    async with make_client() as client:
        headers = {"Authorization": f"Bearer {make_token(admin)}"}

        # 1. Unfiltered: returns both reservations, NEVER blackout
        res = await client.get("/api/v1/admin/bookings?limit=100&offset=0", headers=headers)
        assert res.status_code == 200
        data = res.json()
        listed_ids = [item["id"] for item in data["items"]]
        assert str(b1.id) in listed_ids
        assert str(b2.id) in listed_ids
        assert str(blackout.id) not in listed_ids

        # 2. Filter by resource_id
        res_r1 = await client.get(
            f"/api/v1/admin/bookings?resource_id={resource1.id}", headers=headers
        )
        assert res_r1.status_code == 200
        r1_ids = [item["id"] for item in res_r1.json()["items"]]
        assert str(b1.id) in r1_ids
        assert str(b2.id) not in r1_ids

        # 3. Filter by user_id
        res_u2 = await client.get(f"/api/v1/admin/bookings?user_id={member2.id}", headers=headers)
        assert res_u2.status_code == 200
        u2_ids = [item["id"] for item in res_u2.json()["items"]]
        assert str(b2.id) in u2_ids
        assert str(b1.id) not in u2_ids

        # 4. Filter by status
        res_st = await client.get("/api/v1/admin/bookings?status=confirmed", headers=headers)
        assert res_st.status_code == 200

        # 5. Date filters validation
        # Only starts_at -> 422
        t_s = base_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        res_only_s = await client.get(f"/api/v1/admin/bookings?starts_at={t_s}", headers=headers)
        assert res_only_s.status_code == 422

        # Only ends_at -> 422
        t_e = (base_time + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res_only_e = await client.get(f"/api/v1/admin/bookings?ends_at={t_e}", headers=headers)
        assert res_only_e.status_code == 422

        # ends_at <= starts_at -> 422
        res_rev = await client.get(
            f"/api/v1/admin/bookings?starts_at={t_e}&ends_at={t_s}", headers=headers
        )
        assert res_rev.status_code == 422

        # Window > 90 days -> 422
        t_far = (base_time + timedelta(days=95)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res_wide = await client.get(
            f"/api/v1/admin/bookings?starts_at={t_s}&ends_at={t_far}", headers=headers
        )
        assert res_wide.status_code == 422

        # Valid window -> returns b1 and b2
        res_win = await client.get(
            f"/api/v1/admin/bookings?starts_at={t_s}&ends_at={t_e}", headers=headers
        )
        assert res_win.status_code == 200
        win_ids = [item["id"] for item in res_win.json()["items"]]
        assert str(b1.id) in win_ids
        assert str(b2.id) in win_ids


@pytest.mark.asyncio
async def test_e25_get_admin_booking_detail() -> None:
    admin = await create_user(role="admin")
    member = await create_user(role="member")
    resource = await create_resource()

    base_time = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(
        days=2
    )

    sessionmaker = get_sessionmaker()
    reservation = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=member.id,
        created_by=member.id,
        kind="reservation",
        time_range=Range(base_time, base_time + timedelta(hours=1), bounds="[)"),
        status="confirmed",
        version=1,
    )
    blackout = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=None,
        created_by=admin.id,
        kind="blackout",
        time_range=Range(
            base_time + timedelta(hours=2), base_time + timedelta(hours=3), bounds="[)"
        ),
        status="confirmed",
        version=1,
    )
    async with sessionmaker() as session:
        async with session.begin():
            session.add(reservation)
            session.add(blackout)

    async with make_client() as client:
        headers = {"Authorization": f"Bearer {make_token(admin)}"}

        # 1. Reservation -> 200 with ETag
        res = await client.get(f"/api/v1/admin/bookings/{reservation.id}", headers=headers)
        assert res.status_code == 200
        assert res.json()["id"] == str(reservation.id)
        assert res.json()["kind"] == "reservation"
        assert res.headers.get("ETag") == '"1"'

        # 2. Blackout -> 404 (reservation only!)
        res_blackout = await client.get(f"/api/v1/admin/bookings/{blackout.id}", headers=headers)
        assert res_blackout.status_code == 404
        assert res_blackout.json()["error"]["code"] == "NOT_FOUND"

        # 3. Missing ID -> 404
        res_missing = await client.get(f"/api/v1/admin/bookings/{uuid.uuid4()}", headers=headers)
        assert res_missing.status_code == 404


@pytest.mark.asyncio
async def test_e26_cancel_admin_booking_rules_and_atomicity() -> None:
    admin = await create_user(role="admin")
    member = await create_user(role="member")
    waiter_user = await create_user(role="member")
    resource = await create_resource()

    now = datetime.now(timezone.utc)
    # Future reservation
    future_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(days=2)
    future_end = future_start + timedelta(hours=1)

    # Running reservation (started 30 mins ago, ends in 2.5 hours)
    current_half = now.replace(minute=(0 if now.minute < 30 else 30), second=0, microsecond=0)
    running_start = current_half - timedelta(minutes=30)
    running_end = running_start + timedelta(hours=3)

    # Completed reservation (ended 2 hours ago)
    past_start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    past_end = past_start + timedelta(hours=1)

    sessionmaker = get_sessionmaker()
    b_future = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=member.id,
        created_by=member.id,
        kind="reservation",
        time_range=Range(future_start, future_end, bounds="[)"),
        status="confirmed",
        version=1,
    )
    b_running = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=member.id,
        created_by=member.id,
        kind="reservation",
        time_range=Range(running_start, running_end, bounds="[)"),
        status="confirmed",
        version=1,
    )
    b_past = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=member.id,
        created_by=member.id,
        kind="reservation",
        time_range=Range(past_start, past_end, bounds="[)"),
        status="confirmed",
        version=1,
    )
    # Add a waiting entry for the future slot to test atomic promotion
    waiter = WaitlistEntry(
        id=uuid.uuid4(),
        user_id=waiter_user.id,
        resource_id=resource.id,
        time_range=Range(future_start, future_end, bounds="[)"),
        status="waiting",
        offered_booking_id=None,
        version=1,
    )

    async with sessionmaker() as session:
        async with session.begin():
            session.add(b_future)
            session.add(b_running)
            session.add(b_past)
            session.add(waiter)

    async with make_client() as client:
        headers = {"Authorization": f"Bearer {make_token(admin)}"}

        # 1. Missing If-Match on cancel -> 428
        res_no_if = await client.post(
            f"/api/v1/admin/bookings/{b_future.id}/cancel",
            json={"reason": "admin test"},
            headers=headers,
        )
        assert res_no_if.status_code == 428

        # 2. Stale If-Match -> 412
        res_stale = await client.post(
            f"/api/v1/admin/bookings/{b_future.id}/cancel",
            json={"reason": "admin test"},
            headers={**headers, "If-Match": '"99"'},
        )
        assert res_stale.status_code == 412

        # 3. Past booking cancellation rejected -> 409 TOO_LATE
        res_past = await client.post(
            f"/api/v1/admin/bookings/{b_past.id}/cancel",
            json={"reason": "too late"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert res_past.status_code == 409
        assert res_past.json()["error"]["code"] == "TOO_LATE"

        # 4. Running booking cancellation by admin -> SUCCEEDS (200)
        res_running = await client.post(
            f"/api/v1/admin/bookings/{b_running.id}/cancel",
            json={"reason": "admin cancelling running"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert res_running.status_code == 200
        assert res_running.json()["status"] == "cancelled"
        assert res_running.json()["version"] == 2

        # 5. Future booking cancellation by admin -> SUCCEEDS (200)
        res_future = await client.post(
            f"/api/v1/admin/bookings/{b_future.id}/cancel",
            json={"reason": "freeing slot"},
            headers={**headers, "If-Match": '"1"'},
        )
        assert res_future.status_code == 200
        assert res_future.json()["status"] == "cancelled"

        # Verify atomic audit log, outbox event, and waitlist promotion
        async with sessionmaker() as session:
            # Audit log
            stmt_audit = select(AuditLog).where(
                AuditLog.target_type == "booking",
                AuditLog.target_id == b_future.id,
                AuditLog.action == "admin.booking_cancel",
            )
            audit_entry = (await session.execute(stmt_audit)).scalar_one()
            assert audit_entry.actor_id == admin.id
            assert audit_entry.details["reason_length"] == len("freeing slot")

            # Outbox event
            stmt_outbox = select(Outbox).where(
                Outbox.aggregate_id == b_future.id,
                Outbox.event_type == "booking_cancelled",
            )
            outbox_entry = (await session.execute(stmt_outbox)).scalar_one()
            assert outbox_entry is not None

            # Waiter promoted
            stmt_w = select(WaitlistEntry).where(WaitlistEntry.id == waiter.id)
            w_db = (await session.execute(stmt_w)).scalar_one()
            assert w_db.status == "offered"
            assert w_db.offered_booking_id is not None


@pytest.mark.asyncio
async def test_admin_vs_member_routes_ownership_distinction() -> None:
    admin = await create_user(role="admin")
    member = await create_user(role="member")
    resource = await create_resource()

    base_time = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(
        days=2
    )

    sessionmaker = get_sessionmaker()
    member_booking = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=member.id,
        created_by=member.id,
        kind="reservation",
        time_range=Range(base_time, base_time + timedelta(hours=1), bounds="[)"),
        status="confirmed",
        version=1,
    )
    async with sessionmaker() as session:
        async with session.begin():
            session.add(member_booking)

    async with make_client() as client:
        admin_token = make_token(admin)
        member_token = make_token(member)

        # 1. Admin using member route E10: does NOT see member's booking
        r_e10 = await client.get(
            "/api/v1/bookings", headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert r_e10.status_code == 200
        assert str(member_booking.id) not in [item["id"] for item in r_e10.json()["items"]]

        # 2. Admin using member route E11: foreign booking is 404
        r_e11 = await client.get(
            f"/api/v1/bookings/{member_booking.id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r_e11.status_code == 404

        # 3. Admin using member route E12: foreign booking cancel is 404
        r_e12 = await client.post(
            f"/api/v1/bookings/{member_booking.id}/cancel",
            json={"reason": "admin trying via member route"},
            headers={"Authorization": f"Bearer {admin_token}", "If-Match": '"1"'},
        )
        assert r_e12.status_code == 404

        # 4. Member using admin routes E24, E25, E26 gets 403 FORBIDDEN
        h_mem = {"Authorization": f"Bearer {member_token}"}
        assert (await client.get("/api/v1/admin/bookings", headers=h_mem)).status_code == 403
        assert (
            await client.get(f"/api/v1/admin/bookings/{member_booking.id}", headers=h_mem)
        ).status_code == 403
        assert (
            await client.post(
                f"/api/v1/admin/bookings/{member_booking.id}/cancel",
                json={"reason": "member"},
                headers={**h_mem, "If-Match": '"1"'},
            )
        ).status_code == 403


@pytest.mark.asyncio
async def test_e26_nonexistent_booking_cancel_precondition_precedence() -> None:
    admin = await create_user(role="admin")
    admin_token = make_token(admin)
    absent_id = uuid.uuid4()

    async with make_client() as client:
        h = {"Authorization": f"Bearer {admin_token}"}
        valid_body = {"reason": "cancel test"}

        # 1. Nonexistent booking + missing If-Match -> 428 PRECONDITION_REQUIRED
        r_missing = await client.post(
            f"/api/v1/admin/bookings/{absent_id}/cancel",
            json=valid_body,
            headers=h,
        )
        assert r_missing.status_code == 428
        assert r_missing.json()["error"]["code"] == "PRECONDITION_REQUIRED"

        # 2. Nonexistent booking + malformed If-Match -> 422 VALIDATION_ERROR
        r_malformed = await client.post(
            f"/api/v1/admin/bookings/{absent_id}/cancel",
            json=valid_body,
            headers={**h, "If-Match": "invalid-tag"},
        )
        assert r_malformed.status_code == 422
        assert r_malformed.json()["error"]["code"] == "VALIDATION_ERROR"

        # 3. Nonexistent booking + valid If-Match -> 404 NOT_FOUND
        r_valid = await client.post(
            f"/api/v1/admin/bookings/{absent_id}/cancel",
            json=valid_body,
            headers={**h, "If-Match": '"1"'},
        )
        assert r_valid.status_code == 404
        assert r_valid.json()["error"]["code"] == "NOT_FOUND"
