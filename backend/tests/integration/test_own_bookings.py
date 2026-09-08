"""Behavioral tests for own booking listing, detail and cancellation (E10, E11, E12)."""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-minimum-32-bytes-long-12345678")
os.environ.setdefault("RATE_LIMIT_HMAC_SECRET", "test-hmac-secret-minimum-32-bytes-long-1234")

import app.bookings.service as booking_service
from app.auth.dependencies import AuthorizedScope, Policy
from app.auth.models import RateLimit, User
from app.auth.passwords import hash_password
from app.auth.rate_limit import hash_identity, mutation_limit, read_limit, window_start_for
from app.bookings.models import Booking
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.main import app
from app.notifications.models import Outbox
from app.resources.models import Resource

get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _cleanup_engine() -> Any:
    yield
    import app.db.session

    if app.db.session._engine is not None:
        await app.db.session._engine.dispose()
        app.db.session._engine = None
        app.db.session._sessionmaker = None
        app.db.session._engine_url = None


def make_client(ip: str = "127.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False, client=(ip, 12345)),
        base_url="http://localhost:5173",
    )


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


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(user)}"}


async def create_user(*, role: str = "member", enabled: bool = True) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"own_{uuid.uuid4().hex[:10]}@example.com",
        password_hash=hash_password("valid-password-123"),
        display_name="Own Bookings User",
        role=role,
        enabled=enabled,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(user)
    return user


async def create_resource() -> Resource:
    resource = Resource(
        id=uuid.uuid4(),
        name=f"OwnBookings_{uuid.uuid4().hex[:8]}",
        description="Own bookings suite",
        location="Room 7",
        active=True,
        version=1,
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(resource)
    return resource


def aligned_slot(*, days: int = 2, hour_offset: int = 0) -> tuple[datetime, datetime]:
    """Return a 30-minute aligned one-hour window comfortably inside the booking horizon."""
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    starts_at = base + timedelta(days=days, hours=hour_offset)
    return starts_at, starts_at + timedelta(hours=1)


async def insert_booking(
    *,
    resource: Resource,
    owner: User | None,
    created_by: User,
    starts_at: datetime,
    ends_at: datetime,
    status: str = "confirmed",
    kind: str = "reservation",
    version: int = 1,
    created_at: datetime | None = None,
    cancellation_reason: str | None = None,
    expires_at: datetime | None = None,
) -> Booking:
    """Insert a booking row directly so tests can build states the API cannot create."""
    booking = Booking(
        id=uuid.uuid4(),
        resource_id=resource.id,
        user_id=owner.id if owner is not None else None,
        created_by=created_by.id,
        kind=kind,
        time_range=Range(starts_at, ends_at, bounds="[)"),
        status=status,
        expires_at=expires_at,
        cancellation_reason=cancellation_reason,
        version=version,
    )
    if created_at is not None:
        booking.created_at = created_at
        booking.updated_at = created_at
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(booking)
    return booking


async def read_booking(booking_id: uuid.UUID) -> Booking:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(Booking).where(Booking.id == booking_id)
        return (await session.execute(stmt)).scalar_one()


async def db_clock_now() -> datetime:
    """Sample the same PostgreSQL clock the cancellation deadline is compared against."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        value = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    assert isinstance(value, datetime)
    return value


async def hold_resource_lock(resource: Resource) -> Any:
    """Open a transaction holding the resource row FOR SHARE, as an E09 create does.

    The caller is responsible for rolling the returned session back and closing it. No
    booking row is touched, so anything that blocks behind this holder is blocked on the
    resource lock alone.
    """
    holder = get_sessionmaker()()
    await holder.begin()
    await holder.execute(
        select(Resource).where(Resource.id == resource.id).with_for_update(read=True)
    )
    return holder


async def wait_for_fresh_window(minimum_seconds: float = 20.0) -> None:
    """Wait out a fixed rate-limit window that is about to roll over mid-assertion."""
    remaining = 60 - (datetime.now(timezone.utc).timestamp() % 60)
    if remaining < minimum_seconds:
        await asyncio.sleep(remaining + 0.1)


async def seed_bucket(bucket_scope: str, user_id: uuid.UUID, *, count: int) -> None:
    """Pre-fill a per-user fixed-window bucket so a limit can be reached in two requests."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        async with session.begin():
            session.add(
                RateLimit(
                    scope=bucket_scope,
                    identity_hash=hash_identity(str(user_id)),
                    window_start=window_start_for(datetime.now(timezone.utc)),
                    count=count,
                )
            )


async def bucket_count(bucket_scope: str, user_id: uuid.UUID) -> int:
    """Return the consumed count of a per-user bucket in the current window, 0 if unused."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(RateLimit.count).where(
            RateLimit.scope == bucket_scope,
            RateLimit.identity_hash == hash_identity(str(user_id)),
            RateLimit.window_start == window_start_for(datetime.now(timezone.utc)),
        )
        stored = (await session.execute(stmt)).scalar_one_or_none()
    return int(stored) if stored is not None else 0


async def count_events(booking_id: uuid.UUID, event_type: str) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = (
            select(func.count())
            .select_from(Outbox)
            .where(Outbox.aggregate_id == booking_id, Outbox.event_type == event_type)
        )
        return int((await session.execute(stmt)).scalar_one())


async def create_booking_through_api(
    client: httpx.AsyncClient,
    user: User,
    resource: Resource,
    starts_at: datetime,
    ends_at: datetime,
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/bookings",
        json={
            "resource_id": str(resource.id),
            "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ends_at": ends_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        headers={**auth(user), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# --- E10: GET /api/v1/bookings -------------------------------------------------------


async def test_e10_returns_only_own_reservations_and_never_blackouts() -> None:
    owner = await create_user()
    stranger = await create_user()
    admin = await create_user(role="admin")
    resource = await create_resource()

    starts_at, ends_at = aligned_slot(days=3)
    own = await insert_booking(
        resource=resource, owner=owner, created_by=owner, starts_at=starts_at, ends_at=ends_at
    )
    foreign = await insert_booking(
        resource=resource,
        owner=stranger,
        created_by=stranger,
        starts_at=starts_at + timedelta(hours=2),
        ends_at=ends_at + timedelta(hours=2),
    )
    blackout = await insert_booking(
        resource=resource,
        owner=None,
        created_by=admin,
        starts_at=starts_at + timedelta(hours=4),
        ends_at=ends_at + timedelta(hours=4),
        kind="blackout",
    )

    async with make_client() as client:
        response = await client.get("/api/v1/bookings", headers=auth(owner))

    assert response.status_code == 200
    body = response.json()
    returned_ids = [item["id"] for item in body["items"]]
    assert returned_ids == [str(own.id)]
    assert str(foreign.id) not in returned_ids
    assert str(blackout.id) not in returned_ids
    assert body["total"] == 1
    assert body["limit"] == 25
    assert body["offset"] == 0
    assert body["items"][0]["user_id"] == str(owner.id)
    assert body["items"][0]["kind"] == "reservation"
    assert set(body.keys()) == {"items", "total", "limit", "offset"}
    # Booking bodies carry no identity or delivery fields.
    assert set(body["items"][0].keys()) == {
        "id",
        "resource_id",
        "user_id",
        "kind",
        "starts_at",
        "ends_at",
        "status",
        "expires_at",
        "cancellation_reason",
        "version",
        "created_at",
        "updated_at",
    }


async def test_e10_paginates_in_created_at_descending_order() -> None:
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=4)
    anchor = datetime.now(timezone.utc) - timedelta(hours=3)

    created = []
    for index in range(3):
        created.append(
            await insert_booking(
                resource=resource,
                owner=owner,
                created_by=owner,
                starts_at=starts_at + timedelta(hours=2 * index),
                ends_at=ends_at + timedelta(hours=2 * index),
                created_at=anchor + timedelta(minutes=index),
            )
        )
    newest_first = [str(created[2].id), str(created[1].id), str(created[0].id)]

    async with make_client() as client:
        first_page = await client.get("/api/v1/bookings?limit=2&offset=0", headers=auth(owner))
        second_page = await client.get("/api/v1/bookings?limit=2&offset=2", headers=auth(owner))
        over_limit = await client.get("/api/v1/bookings?limit=101", headers=auth(owner))

    assert first_page.status_code == 200
    assert [item["id"] for item in first_page.json()["items"]] == newest_first[:2]
    assert first_page.json()["total"] == 3
    assert first_page.json()["limit"] == 2
    assert first_page.json()["offset"] == 0

    assert second_page.status_code == 200
    assert [item["id"] for item in second_page.json()["items"]] == newest_first[2:]
    assert second_page.json()["total"] == 3
    assert second_page.json()["offset"] == 2

    assert over_limit.status_code == 422
    assert over_limit.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_e10_status_filter_selects_matching_reservations() -> None:
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=5)

    confirmed = await insert_booking(
        resource=resource, owner=owner, created_by=owner, starts_at=starts_at, ends_at=ends_at
    )
    cancelled = await insert_booking(
        resource=resource,
        owner=owner,
        created_by=owner,
        starts_at=starts_at + timedelta(hours=2),
        ends_at=ends_at + timedelta(hours=2),
        status="cancelled",
        version=2,
        cancellation_reason="plans changed",
    )

    async with make_client() as client:
        confirmed_page = await client.get("/api/v1/bookings?status=confirmed", headers=auth(owner))
        cancelled_page = await client.get("/api/v1/bookings?status=cancelled", headers=auth(owner))
        unknown_status = await client.get("/api/v1/bookings?status=nonsense", headers=auth(owner))

    assert [item["id"] for item in confirmed_page.json()["items"]] == [str(confirmed.id)]
    assert confirmed_page.json()["total"] == 1
    assert [item["id"] for item in cancelled_page.json()["items"]] == [str(cancelled.id)]
    assert cancelled_page.json()["items"][0]["cancellation_reason"] == "plans changed"
    assert unknown_status.status_code == 422
    assert unknown_status.json()["error"]["code"] == "VALIDATION_ERROR"


# --- E11: GET /api/v1/bookings/{id} --------------------------------------------------


async def test_e11_returns_owner_booking_with_etag() -> None:
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=6)
    booking = await insert_booking(
        resource=resource, owner=owner, created_by=owner, starts_at=starts_at, ends_at=ends_at
    )

    async with make_client() as client:
        response = await client.get(f"/api/v1/bookings/{booking.id}", headers=auth(owner))

    assert response.status_code == 200
    assert response.json()["id"] == str(booking.id)
    assert response.json()["status"] == "confirmed"
    assert response.headers["etag"] == '"1"'
    assert response.json()["starts_at"].endswith("Z")


async def test_e11_foreign_unknown_and_blackout_ids_are_not_found() -> None:
    owner = await create_user()
    stranger = await create_user()
    admin = await create_user(role="admin")
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=7)

    foreign = await insert_booking(
        resource=resource,
        owner=stranger,
        created_by=stranger,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    blackout = await insert_booking(
        resource=resource,
        owner=None,
        created_by=admin,
        starts_at=starts_at + timedelta(hours=2),
        ends_at=ends_at + timedelta(hours=2),
        kind="blackout",
    )

    async with make_client() as client:
        foreign_read = await client.get(f"/api/v1/bookings/{foreign.id}", headers=auth(owner))
        unknown_read = await client.get(f"/api/v1/bookings/{uuid.uuid4()}", headers=auth(owner))
        blackout_read = await client.get(f"/api/v1/bookings/{blackout.id}", headers=auth(admin))
        # An admin has no own-scope access to another member's reservation.
        admin_read = await client.get(f"/api/v1/bookings/{foreign.id}", headers=auth(admin))

    for response in (foreign_read, unknown_read, blackout_read, admin_read):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


# --- E12: POST /api/v1/bookings/{id}/cancel ------------------------------------------


async def test_e12_cancels_confirmed_booking_and_releases_the_slot() -> None:
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=8)

    async with make_client() as client:
        created = await create_booking_through_api(client, owner, resource, starts_at, ends_at)
        booking_id = uuid.UUID(created["id"])

        response = await client.post(
            f"/api/v1/bookings/{booking_id}/cancel",
            json={"reason": "no longer needed"},
            headers={**auth(owner), "If-Match": '"1"'},
        )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
        assert response.json()["cancellation_reason"] == "no longer needed"
        assert response.json()["expires_at"] is None
        assert response.json()["version"] == 2
        assert response.headers["etag"] == '"2"'

        stored = await read_booking(booking_id)
        assert stored.status == "cancelled"
        assert stored.version == 2
        assert stored.expires_at is None
        assert stored.cancellation_reason == "no longer needed"
        assert await count_events(booking_id, "booking_cancelled") == 1

        # The released interval is immediately bookable again through E09.
        rebooked = await create_booking_through_api(client, owner, resource, starts_at, ends_at)

    assert rebooked["id"] != created["id"]
    assert rebooked["status"] == "confirmed"


async def test_e12_if_match_matrix() -> None:
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=9)
    booking = await insert_booking(
        resource=resource, owner=owner, created_by=owner, starts_at=starts_at, ends_at=ends_at
    )
    cancelled = await insert_booking(
        resource=resource,
        owner=owner,
        created_by=owner,
        starts_at=starts_at + timedelta(hours=2),
        ends_at=ends_at + timedelta(hours=2),
        status="cancelled",
        version=2,
    )
    started = await insert_booking(
        resource=resource,
        owner=owner,
        created_by=owner,
        starts_at=datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        - timedelta(hours=2),
        ends_at=datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        - timedelta(hours=1),
    )
    started_version_two = await insert_booking(
        resource=resource,
        owner=owner,
        created_by=owner,
        starts_at=datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        - timedelta(hours=5),
        ends_at=datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        - timedelta(hours=4),
        version=2,
    )

    async with make_client() as client:
        missing = await client.post(
            f"/api/v1/bookings/{booking.id}/cancel", json={"reason": ""}, headers=auth(owner)
        )
        assert missing.status_code == 428
        assert missing.json()["error"]["code"] == "PRECONDITION_REQUIRED"

        for malformed_value in ("1", 'W/"1"', "*", '""', '"1", "2"', '"01"'):
            malformed = await client.post(
                f"/api/v1/bookings/{booking.id}/cancel",
                json={"reason": ""},
                headers={**auth(owner), "If-Match": malformed_value},
            )
            assert malformed.status_code == 422, malformed_value
            assert malformed.json()["error"]["code"] == "VALIDATION_ERROR"

        stale = await client.post(
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": ""},
            headers={**auth(owner), "If-Match": '"7"'},
        )
        assert stale.status_code == 412
        assert stale.json()["error"]["code"] == "VERSION_MISMATCH"

        # A stale precondition outranks the terminal-state answer.
        stale_on_cancelled = await client.post(
            f"/api/v1/bookings/{cancelled.id}/cancel",
            json={"reason": ""},
            headers={**auth(owner), "If-Match": '"1"'},
        )
        assert stale_on_cancelled.status_code == 412
        assert stale_on_cancelled.json()["error"]["code"] == "VERSION_MISMATCH"

        # A stale precondition also outranks the too-late answer for a started booking.
        stale_on_started = await client.post(
            f"/api/v1/bookings/{started.id}/cancel",
            json={"reason": ""},
            headers={**auth(owner), "If-Match": '"9"'},
        )
        assert stale_on_started.status_code == 412
        assert stale_on_started.json()["error"]["code"] == "VERSION_MISMATCH"

        current_on_started = await client.post(
            f"/api/v1/bookings/{started_version_two.id}/cancel",
            json={"reason": ""},
            headers={**auth(owner), "If-Match": '"2"'},
        )
        assert current_on_started.status_code == 409
        assert current_on_started.json()["error"]["code"] == "TOO_LATE"

    unchanged = await read_booking(booking.id)
    assert unchanged.status == "confirmed"
    assert unchanged.version == 1


async def test_e12_current_version_on_cancelled_booking_is_idempotent() -> None:
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=10)

    async with make_client() as client:
        created = await create_booking_through_api(client, owner, resource, starts_at, ends_at)
        booking_id = uuid.UUID(created["id"])

        first = await client.post(
            f"/api/v1/bookings/{booking_id}/cancel",
            json={"reason": "first"},
            headers={**auth(owner), "If-Match": '"1"'},
        )
        assert first.status_code == 200
        assert first.json()["version"] == 2

        repeat = await client.post(
            f"/api/v1/bookings/{booking_id}/cancel",
            json={"reason": "second attempt"},
            headers={**auth(owner), "If-Match": '"2"'},
        )

    assert repeat.status_code == 200
    assert repeat.json()["version"] == 2
    assert repeat.json()["status"] == "cancelled"
    # The stored reason from the original cancellation is preserved.
    assert repeat.json()["cancellation_reason"] == "first"

    stored = await read_booking(booking_id)
    assert stored.version == 2
    assert stored.cancellation_reason == "first"
    assert await count_events(booking_id, "booking_cancelled") == 1


async def test_e12_rejects_cancellation_of_a_booking_that_started_an_hour_ago() -> None:
    owner = await create_user()
    resource = await create_resource()
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    booking = await insert_booking(
        resource=resource,
        owner=owner,
        created_by=owner,
        starts_at=base - timedelta(hours=1),
        ends_at=base + timedelta(hours=1),
    )

    async with make_client() as client:
        response = await client.post(
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": "too late now"},
            headers={**auth(owner), "If-Match": '"1"'},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TOO_LATE"

    stored = await read_booking(booking.id)
    assert stored.status == "confirmed"
    assert stored.version == 1
    assert stored.cancellation_reason is None
    assert await count_events(booking.id, "booking_cancelled") == 0


async def test_e12_rejects_cancellation_of_expired_booking() -> None:
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=11)
    booking = await insert_booking(
        resource=resource,
        owner=owner,
        created_by=owner,
        starts_at=starts_at,
        ends_at=ends_at,
        status="expired",
    )

    async with make_client() as client:
        response = await client.post(
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": ""},
            headers={**auth(owner), "If-Match": '"1"'},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"

    stored = await read_booking(booking.id)
    assert stored.status == "expired"
    assert stored.version == 1


async def test_e12_simultaneous_cancellations_produce_one_success() -> None:
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=12)
    booking = await insert_booking(
        resource=resource, owner=owner, created_by=owner, starts_at=starts_at, ends_at=ends_at
    )

    async def cancel(client: httpx.AsyncClient, reason: str) -> httpx.Response:
        return await client.post(
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": reason},
            headers={**auth(owner), "If-Match": '"1"'},
        )

    async with make_client() as first_client, make_client() as second_client:
        responses = await asyncio.gather(
            cancel(first_client, "contender one"),
            cancel(second_client, "contender two"),
        )

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 412]
    losing = next(r for r in responses if r.status_code == 412)
    assert losing.json()["error"]["code"] == "VERSION_MISMATCH"

    stored = await read_booking(booking.id)
    assert stored.status == "cancelled"
    assert stored.version == 2
    assert await count_events(booking.id, "booking_cancelled") == 1


async def test_e12_failure_before_commit_leaves_no_partial_state() -> None:
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=13)
    booking = await insert_booking(
        resource=resource, owner=owner, created_by=owner, starts_at=starts_at, ends_at=ends_at
    )

    real_append_event = booking_service.append_event

    async def failing_append_event(session: Any, **kwargs: Any) -> uuid.UUID:
        await real_append_event(session, **kwargs)
        raise RuntimeError("injected failure after the booking update, before commit")

    booking_service.append_event = failing_append_event  # type: ignore[assignment]
    try:
        async with make_client() as client:
            response = await client.post(
                f"/api/v1/bookings/{booking.id}/cancel",
                json={"reason": "rolled back"},
                headers={**auth(owner), "If-Match": '"1"'},
            )
    finally:
        booking_service.append_event = real_append_event  # type: ignore[assignment]

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"

    stored = await read_booking(booking.id)
    assert stored.status == "confirmed"
    assert stored.version == 1
    assert stored.cancellation_reason is None
    assert await count_events(booking.id, "booking_cancelled") == 0


async def test_e12_cancels_an_offered_booking_and_clears_its_expiry() -> None:
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=14)
    booking = await insert_booking(
        resource=resource,
        owner=owner,
        created_by=owner,
        starts_at=starts_at,
        ends_at=ends_at,
        status="offered",
        expires_at=starts_at - timedelta(minutes=15),
    )
    assert booking.expires_at is not None

    async with make_client() as client:
        response = await client.post(
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": "declining the hold"},
            headers={**auth(owner), "If-Match": '"1"'},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert response.json()["expires_at"] is None
    assert response.json()["version"] == 2
    assert response.json()["cancellation_reason"] == "declining the hold"
    assert response.headers["etag"] == '"2"'

    stored = await read_booking(booking.id)
    assert stored.status == "cancelled"
    assert stored.expires_at is None
    assert stored.version == 2
    assert await count_events(booking.id, "booking_cancelled") == 1


async def test_e12_start_boundary_allows_just_before_and_refuses_just_after() -> None:
    """The deadline is judged against the database clock, seconds either side of start."""
    owner = await create_user()
    started_resource = await create_resource()
    upcoming_resource = await create_resource()

    sampled = await db_clock_now()
    just_started = await insert_booking(
        resource=started_resource,
        owner=owner,
        created_by=owner,
        starts_at=sampled - timedelta(seconds=1),
        ends_at=sampled + timedelta(hours=1),
    )
    about_to_start = await insert_booking(
        resource=upcoming_resource,
        owner=owner,
        created_by=owner,
        starts_at=sampled + timedelta(seconds=30),
        ends_at=sampled + timedelta(hours=1),
    )

    async with make_client() as client:
        too_late = await client.post(
            f"/api/v1/bookings/{just_started.id}/cancel",
            json={"reason": "one second too late"},
            headers={**auth(owner), "If-Match": '"1"'},
        )
        in_time = await client.post(
            f"/api/v1/bookings/{about_to_start.id}/cancel",
            json={"reason": "just in time"},
            headers={**auth(owner), "If-Match": '"1"'},
        )

    assert too_late.status_code == 409, too_late.text
    assert too_late.json()["error"]["code"] == "TOO_LATE"
    assert in_time.status_code == 200, in_time.text
    assert in_time.json()["status"] == "cancelled"

    unchanged = await read_booking(just_started.id)
    assert unchanged.status == "confirmed"
    assert unchanged.version == 1
    assert await count_events(just_started.id, "booking_cancelled") == 0

    cancelled = await read_booking(about_to_start.id)
    assert cancelled.status == "cancelled"
    assert cancelled.version == 2
    assert await count_events(about_to_start.id, "booking_cancelled") == 1


async def test_e12_waits_for_the_resource_lock_before_reading_the_booking() -> None:
    """Cancellation takes the resource FOR UPDATE first, so it queues behind a create."""
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=16)
    booking = await insert_booking(
        resource=resource, owner=owner, created_by=owner, starts_at=starts_at, ends_at=ends_at
    )

    holder = await hold_resource_lock(resource)
    try:
        async with make_client() as client:
            cancel = asyncio.create_task(
                client.post(
                    f"/api/v1/bookings/{booking.id}/cancel",
                    json={"reason": "queued behind a create"},
                    headers={**auth(owner), "If-Match": '"1"'},
                )
            )
            await asyncio.sleep(2)

            # Nothing holds the booking row, so the only lock that can be blocking the
            # cancellation is the resource FOR UPDATE it must acquire first.
            assert not cancel.done()
            during = await read_booking(booking.id)
            assert during.status == "confirmed"
            assert during.version == 1

            await holder.rollback()
            response = await asyncio.wait_for(cancel, timeout=30)
    finally:
        await holder.close()

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert response.json()["version"] == 2
    assert await count_events(booking.id, "booking_cancelled") == 1

    # The interval the cancellation released is bookable again through E09.
    async with make_client() as client:
        rebooked = await create_booking_through_api(client, owner, resource, starts_at, ends_at)
    assert rebooked["status"] == "confirmed"
    assert rebooked["id"] != str(booking.id)


async def test_e12_deadline_uses_the_clock_sampled_after_the_lock_wait() -> None:
    """A booking that starts while the cancellation waits for its locks is TOO_LATE."""
    owner = await create_user()
    resource = await create_resource()

    sampled = await db_clock_now()
    booking = await insert_booking(
        resource=resource,
        owner=owner,
        created_by=owner,
        starts_at=sampled + timedelta(seconds=3),
        ends_at=sampled + timedelta(hours=1),
    )

    holder = await hold_resource_lock(resource)
    try:
        async with make_client() as client:
            cancel = asyncio.create_task(
                client.post(
                    f"/api/v1/bookings/{booking.id}/cancel",
                    json={"reason": "started during the wait"},
                    headers={**auth(owner), "If-Match": '"1"'},
                )
            )
            # Well inside the 15 second lock timeout, and well past the booking's start.
            await asyncio.sleep(6)
            assert not cancel.done()

            await holder.rollback()
            response = await asyncio.wait_for(cancel, timeout=30)
    finally:
        await holder.close()

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "TOO_LATE"

    stored = await read_booking(booking.id)
    assert stored.status == "confirmed"
    assert stored.version == 1
    assert stored.cancellation_reason is None
    assert await count_events(booking.id, "booking_cancelled") == 0


# --- Rate limiting -------------------------------------------------------------------


async def test_read_bucket_rejects_the_request_past_its_ceiling() -> None:
    owner = await create_user()
    await wait_for_fresh_window()
    limit = read_limit()
    await seed_bucket("read:user", owner.id, count=limit - 1)

    async with make_client() as client:
        last_allowed = await client.get("/api/v1/bookings", headers=auth(owner))
        rejected = await client.get("/api/v1/bookings", headers=auth(owner))

    assert last_allowed.status_code == 200, last_allowed.text
    assert rejected.status_code == 429, rejected.text
    assert rejected.json()["error"]["code"] == "RATE_LIMITED"

    retry_after = rejected.headers["retry-after"]
    assert retry_after == str(int(retry_after))
    assert 1 <= int(retry_after) <= 60


async def test_e12_cancel_consumes_the_mutation_bucket() -> None:
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=17)
    booking = await insert_booking(
        resource=resource, owner=owner, created_by=owner, starts_at=starts_at, ends_at=ends_at
    )

    await wait_for_fresh_window()
    assert await bucket_count("mutation:user", owner.id) == 0

    async with make_client() as client:
        response = await client.post(
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": "metered"},
            headers={**auth(owner), "If-Match": '"1"'},
        )

    assert response.status_code == 200, response.text
    assert await bucket_count("mutation:user", owner.id) == 1
    # A cancellation spends the mutation budget only.
    assert await bucket_count("read:user", owner.id) == 0


async def test_rejected_own_booking_requests_still_consume_their_budget() -> None:
    """404, 428 and 422 answers are metered, so identifier probing is not free."""
    reader = await create_user()
    canceller = await create_user()
    await wait_for_fresh_window()

    async with make_client() as client:
        unknown_read = await client.get(f"/api/v1/bookings/{uuid.uuid4()}", headers=auth(reader))
        assert unknown_read.status_code == 404
        assert unknown_read.json()["error"]["code"] == "NOT_FOUND"

        missing_if_match = await client.post(
            f"/api/v1/bookings/{uuid.uuid4()}/cancel",
            json={"reason": ""},
            headers=auth(canceller),
        )
        assert missing_if_match.status_code == 428

        malformed_if_match = await client.post(
            f"/api/v1/bookings/{uuid.uuid4()}/cancel",
            json={"reason": ""},
            headers={**auth(canceller), "If-Match": "3"},
        )
        assert malformed_if_match.status_code == 422

        unknown_cancel = await client.post(
            f"/api/v1/bookings/{uuid.uuid4()}/cancel",
            json={"reason": ""},
            headers={**auth(canceller), "If-Match": '"1"'},
        )
        assert unknown_cancel.status_code == 404

    assert await bucket_count("read:user", reader.id) == 1
    assert await bucket_count("mutation:user", canceller.id) == 3
    assert await bucket_count("read:user", canceller.id) == 0


async def test_rejected_reads_can_exhaust_the_read_bucket() -> None:
    owner = await create_user()
    await wait_for_fresh_window()
    limit = read_limit()
    await seed_bucket("read:user", owner.id, count=limit - 1)

    async with make_client() as client:
        not_found = await client.get(f"/api/v1/bookings/{uuid.uuid4()}", headers=auth(owner))
        rejected = await client.get(f"/api/v1/bookings/{uuid.uuid4()}", headers=auth(owner))

    assert not_found.status_code == 404
    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "RATE_LIMITED"


async def test_mutation_bucket_ceiling_matches_the_configured_profile() -> None:
    owner = await create_user()
    await wait_for_fresh_window()
    limit = mutation_limit()
    await seed_bucket("mutation:user", owner.id, count=limit)

    async with make_client() as client:
        rejected = await client.post(
            f"/api/v1/bookings/{uuid.uuid4()}/cancel",
            json={"reason": ""},
            headers={**auth(owner), "If-Match": '"1"'},
        )

    assert rejected.status_code == 429, rejected.text
    assert rejected.json()["error"]["code"] == "RATE_LIMITED"
    assert int(rejected.headers["retry-after"]) >= 1


# --- Input validation ----------------------------------------------------------------


async def test_own_booking_routes_reject_invalid_inputs() -> None:
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=18)
    booking = await insert_booking(
        resource=resource, owner=owner, created_by=owner, starts_at=starts_at, ends_at=ends_at
    )

    async with make_client() as client:
        malformed_path = await client.get("/api/v1/bookings/not-a-uuid", headers=auth(owner))
        assert malformed_path.status_code == 422, malformed_path.text
        assert malformed_path.json()["error"]["code"] == "VALIDATION_ERROR"
        locations = [error["loc"] for error in malformed_path.json()["error"]["details"]["errors"]]
        assert ["path", "id"] in locations

        pending_filter = await client.get("/api/v1/bookings?status=pending", headers=auth(owner))
        assert pending_filter.status_code == 422, pending_filter.text
        assert pending_filter.json()["error"]["code"] == "VALIDATION_ERROR"

        beyond_offset = await client.get("/api/v1/bookings?offset=10001", headers=auth(owner))
        assert beyond_offset.status_code == 422, beyond_offset.text
        assert beyond_offset.json()["error"]["code"] == "VALIDATION_ERROR"

        long_reason = await client.post(
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": "x" * 501},
            headers={**auth(owner), "If-Match": '"1"'},
        )
        assert long_reason.status_code == 422, long_reason.text
        assert long_reason.json()["error"]["code"] == "VALIDATION_ERROR"

        # The 500-character boundary itself is accepted.
        accepted = await client.post(
            f"/api/v1/bookings/{booking.id}/cancel",
            json={"reason": "y" * 500},
            headers={**auth(owner), "If-Match": '"1"'},
        )
        assert accepted.status_code == 200, accepted.text

    stored = await read_booking(booking.id)
    assert stored.status == "cancelled"
    assert stored.cancellation_reason == "y" * 500
    assert stored.version == 2


# --- D-T013-01: Scope predicates regression ------------------------------------------


async def test_service_applies_scope_predicates_without_rederiving_ownership() -> None:
    """Service applies scope-supplied predicates rather than hardcoding

    user_id == principal_id (Spec 8.1, 3.4).
    """
    owner_a = await create_user()
    owner_b = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=22)
    booking_a = await insert_booking(
        resource=resource,
        owner=owner_a,
        created_by=owner_a,
        starts_at=starts_at,
        ends_at=ends_at,
    )

    sessionmaker = get_sessionmaker()

    # 1. Calling _list_own_bookings with scope.principal_id = owner_b but predicates
    # restricting to owner_a returns booking_a because service applies scope predicates.
    scope_pred_a = AuthorizedScope(
        principal_id=owner_b.id,
        policy=Policy.own_booking,
        predicates={"booking": (Booking.user_id == owner_a.id,)},
    )
    async with sessionmaker() as session:
        page = await booking_service._list_own_bookings(session, scope=scope_pred_a)
        assert any(item.id == booking_a.id for item in page.items)

    # 2. Calling _list_own_bookings with scope.principal_id = owner_a but predicates
    # restricting to owner_b returns EMPTY items because service applies predicates (owner_b).
    scope_pred_b = AuthorizedScope(
        principal_id=owner_a.id,
        policy=Policy.own_booking,
        predicates={"booking": (Booking.user_id == owner_b.id,)},
    )
    async with sessionmaker() as session:
        page = await booking_service._list_own_bookings(session, scope=scope_pred_b)
        assert not any(item.id == booking_a.id for item in page.items)

    # 3. Calling _get_own_booking with scope.principal_id = owner_a but predicates
    # restricting to owner_b raises NotFoundError.
    scope_get_b = AuthorizedScope(
        principal_id=owner_a.id,
        policy=Policy.own_booking,
        object_id=booking_a.id,
        predicates={"booking": (Booking.user_id == owner_b.id,)},
    )
    async with sessionmaker() as session:
        with pytest.raises(booking_service.NotFoundError):
            await booking_service._get_own_booking(session, scope=scope_get_b)

    # 4. Calling cancel_booking with scope.principal_id = owner_a but predicates
    # restricting to owner_b raises NotFoundError.
    scope_cancel_b = AuthorizedScope(
        principal_id=owner_a.id,
        policy=Policy.own_booking,
        object_id=booking_a.id,
        resource_id=resource.id,
        expected_version=1,
        predicates={"booking": (Booking.user_id == owner_b.id,)},
    )
    async with sessionmaker() as session:
        async with session.begin():
            with pytest.raises(booking_service.NotFoundError):
                await booking_service.cancel_booking(
                    session,
                    scope=scope_cancel_b,
                    booking_id=booking_a.id,
                    expected_version=1,
                    reason="scope predicate test",
                    now=datetime.now(timezone.utc),
                )


async def test_owner_scoped_booking_operations_fail_closed_without_predicates() -> None:
    """Owner-scoped booking operations fail closed when scope carries no predicates (R2)."""
    owner = await create_user()
    resource = await create_resource()
    starts_at, ends_at = aligned_slot(days=23)
    booking = await insert_booking(
        resource=resource,
        owner=owner,
        created_by=owner,
        starts_at=starts_at,
        ends_at=ends_at,
    )

    sessionmaker = get_sessionmaker()

    # Scope with empty predicates mapping
    empty_scope = AuthorizedScope(
        principal_id=owner.id,
        policy=Policy.own_booking,
        object_id=booking.id,
        resource_id=resource.id,
        expected_version=1,
    )

    async with sessionmaker() as session:
        # _list_own_bookings must fail closed
        with pytest.raises(RuntimeError, match="requires dependency-supplied predicate"):
            await booking_service._list_own_bookings(session, scope=empty_scope)

        # _get_own_booking must fail closed
        with pytest.raises(RuntimeError, match="requires dependency-supplied predicate"):
            await booking_service._get_own_booking(session, scope=empty_scope)

        # cancel_booking must fail closed
        async with session.begin():
            with pytest.raises(RuntimeError, match="requires dependency-supplied predicate"):
                await booking_service.cancel_booking(
                    session,
                    scope=empty_scope,
                    booking_id=booking.id,
                    expected_version=1,
                    reason="fail-closed test",
                    now=datetime.now(timezone.utc),
                )
