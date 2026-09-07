import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthorizedScope, AuthRequiredError, Policy, authorize
from app.auth.rate_limit import (
    RateLimitBucket,
    consume_rate_limits,
    hash_identity,
)
from app.bookings.idempotency import _parse_idempotency_key, execute_create
from app.bookings.schemas import Booking as BookingSchema
from app.bookings.schemas import BookingCreate, StoredResponse
from app.bookings.service import (
    ResourceInactive,
    SlotConflict,
    insert_confirmed,
    validate_booking_window,
)
from app.config import get_settings
from app.db.session import transaction_dependency
from app.notifications.outbox import append_event

router = APIRouter()


async def _check_mutation_rate_limit(user_id: uuid.UUID, now: datetime) -> None:
    """Consume rate limits for authenticated mutations: 120/user/min (1000 in race profile)."""
    settings = get_settings()
    limit = 1000 if settings.TEST_PROFILE == "race" else 120

    user_hash = hash_identity(str(user_id))
    timestamp = int(now.timestamp())
    window_seconds = 60
    start_epoch = timestamp - (timestamp % window_seconds)
    window_start = datetime.fromtimestamp(start_epoch, tz=timezone.utc)

    buckets = [
        RateLimitBucket(
            scope="mutation:user",
            identity_hash=user_hash,
            window_start=window_start,
            limit=limit,
            window_seconds=60,
        )
    ]
    await consume_rate_limits(buckets)


@router.post("/bookings", status_code=201)
async def create_booking_endpoint(
    request: Request,
    body: BookingCreate,
    idempotency_key_header: str | None = Header(None, alias="Idempotency-Key"),
    scope: AuthorizedScope = Depends(authorize(Policy.authenticated)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> Response:
    # 1. Parse and validate Idempotency-Key header BEFORE any domain work
    key = _parse_idempotency_key(idempotency_key_header)

    if scope.principal_id is None:
        raise AuthRequiredError("Authentication required")

    now = datetime.now(timezone.utc)

    # 2. Consume rate limits in its own short independent transaction BEFORE domain transaction
    await _check_mutation_rate_limit(scope.principal_id, now=now)

    # 3. Revalidate user and acquire FOR SHARE lock inside domain session (Spec 3.4, 5.1)
    if scope.assert_current is not None:
        await scope.assert_current(session)

    async def operation() -> StoredResponse:
        # Sample database clock for window validation and persistence
        db_clock = await session.scalar(select(func.clock_timestamp()))
        db_now = db_clock if db_clock is not None else now

        # Dynamic window validation using database clock
        starts_at_utc, ends_at_utc = validate_booking_window(body.starts_at, body.ends_at, db_now)

        try:
            booking = await insert_confirmed(
                session,
                scope=scope,
                resource_id=body.resource_id,
                time_range=Range(starts_at_utc, ends_at_utc, bounds="[)"),
                kind="reservation",
                now=db_now,
            )
        except SlotConflict as exc:
            return StoredResponse(
                status=409,
                body={
                    "error": {
                        "code": "SLOT_CONFLICT",
                        "message": exc.message,
                        "details": {},
                    }
                },
                headers={},
            )
        except ResourceInactive as exc:
            return StoredResponse(
                status=409,
                body={
                    "error": {
                        "code": "RESOURCE_INACTIVE",
                        "message": exc.message,
                        "details": {},
                    }
                },
                headers={},
            )

        # Append transactional outbox event
        await append_event(
            session,
            event_type="booking_confirmed",
            booking=booking,
            now=db_now,
        )

        # Serialize Booking response
        booking_schema = BookingSchema.model_validate(booking)
        booking_dict = booking_schema.model_dump(mode="json")
        headers = {
            "Location": f"/api/v1/bookings/{booking.id}",
            "ETag": f'"{booking.version}"',
        }
        return StoredResponse(
            status=201,
            body=booking_dict,
            headers=headers,
        )

    stored = await execute_create(
        session,
        scope=scope,
        key=key,
        method=request.method,
        path=request.url.path,
        body=body,
        operation=operation,
        now=now,
    )

    return JSONResponse(
        status_code=stored.status,
        content=stored.body,
        headers=stored.headers,
    )
