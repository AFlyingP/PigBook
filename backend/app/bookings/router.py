import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthorizedScope, AuthRequiredError, Policy, authorize
from app.auth.rate_limit import check_mutation_rate_limit
from app.bookings.idempotency import _parse_idempotency_key, execute_create
from app.bookings.schemas import Booking as BookingSchema
from app.bookings.schemas import BookingCreate, BookingStatusFilter, Cancel, StoredResponse
from app.bookings.service import (
    ResourceInactive,
    SlotConflict,
    _get_own_booking,
    _list_own_bookings,
    cancel_booking,
    insert_confirmed,
    validate_booking_window,
)
from app.db.session import get_session, transaction_dependency
from app.notifications.outbox import append_event
from app.resources.schemas import Page

router = APIRouter()


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
    await check_mutation_rate_limit(scope.principal_id, now)

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


@router.get("/bookings", response_model=Page[BookingSchema])
async def list_bookings_endpoint(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    status: BookingStatusFilter | None = Query(default=None),
    scope: AuthorizedScope = Depends(authorize(Policy.own_booking)),
    session: AsyncSession = Depends(get_session),
) -> Page[BookingSchema]:
    """List the caller's own reservations, ordered by created_at descending then id.

    The read budget for this route is consumed by the authorization dependency.
    """
    if scope.principal_id is None:
        raise AuthRequiredError("Authentication required")

    return await _list_own_bookings(
        session,
        scope=scope,
        limit=limit,
        offset=offset,
        status=status,
    )


@router.get("/bookings/{id}", response_model=BookingSchema)
async def get_booking_endpoint(
    id: uuid.UUID,
    response: Response,
    scope: AuthorizedScope = Depends(authorize(Policy.own_booking)),
    session: AsyncSession = Depends(get_session),
) -> BookingSchema:
    """Retrieve one of the caller's own reservations, emitting its version as an ETag.

    The read budget for this route is consumed by the authorization dependency.
    """
    if scope.principal_id is None:
        raise AuthRequiredError("Authentication required")

    booking = await _get_own_booking(session, scope=scope)
    response.headers["ETag"] = f'"{booking.version}"'
    return booking


@router.post("/bookings/{id}/cancel", response_model=None)
async def cancel_booking_endpoint(
    id: uuid.UUID,
    body: Cancel,
    scope: AuthorizedScope = Depends(authorize(Policy.own_booking)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> Response:
    """Cancel one of the caller's own reservations at the version pinned by If-Match.

    The mutation budget for this route is consumed by the authorization dependency, in
    its own transaction, before ownership is resolved.
    """
    if scope.principal_id is None:
        raise AuthRequiredError("Authentication required")
    if scope.object_id is None or scope.expected_version is None:
        # The dependency always resolves both for a POST carrying a path id, so this is a
        # server-side invariant failure rather than anything the caller can correct.
        raise RuntimeError("own_booking scope for a cancel is missing its object identity")

    now = datetime.now(timezone.utc)

    # In-transaction policy revalidation locks the acting user FOR SHARE before the
    # resource and booking rows (Spec 5.1 lock order).
    if scope.assert_current is not None:
        await scope.assert_current(session)

    booking = await cancel_booking(
        session,
        scope=scope,
        booking_id=scope.object_id,
        expected_version=scope.expected_version,
        reason=body.reason,
        now=now,
    )

    return JSONResponse(
        status_code=200,
        content=booking.model_dump(mode="json"),
        headers={"ETag": f'"{booking.version}"'},
    )
