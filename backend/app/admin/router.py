import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.schemas import (
    BlackoutCreate,
    EmptyBody,
    InviteCreate,
    ResourceCreate,
    ResourcePatch,
)
from app.admin.service import (
    archive_resource,
    cancel_blackout,
    create_blackout,
    create_invitation,
    create_resource,
    get_admin_booking,
    list_admin_bookings,
    list_admin_resources,
    list_resource_blackouts,
    update_resource,
)
from app.auth.dependencies import AuthorizedScope, Policy, authorize
from app.auth.schemas import InvitationResult
from app.bookings.idempotency import _parse_idempotency_key, execute_create
from app.bookings.schemas import Booking as BookingSchema
from app.bookings.schemas import BookingStatusFilter, Cancel, StoredResponse
from app.bookings.service import (
    ResourceInactive,
    SlotConflict,
    cancel_booking,
)
from app.db.session import get_session, transaction_dependency
from app.resources.schemas import Page, Resource

router = APIRouter()


# --- Phase 1: Administrator Resource Management (E17–E20) ---


@router.get("/admin/resources", response_model=Page[Resource])
async def list_admin_resources_endpoint(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    active: bool | None = Query(default=None),
    scope: AuthorizedScope = Depends(authorize(Policy.admin)),
    session: AsyncSession = Depends(get_session),
) -> Page[Resource]:
    """List all resources (active and inactive) ordered by name ascending, then id (E17)."""
    return await list_admin_resources(
        session,
        scope=scope,
        limit=limit,
        offset=offset,
        active=active,
    )


@router.post("/admin/resources", response_model=Resource, status_code=201)
async def create_resource_endpoint(
    body: ResourceCreate,
    response: Response,
    scope: AuthorizedScope = Depends(authorize(Policy.admin)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> Resource:
    """Create a new resource (E18)."""
    now = datetime.now(timezone.utc)
    if scope.assert_current is not None:
        await scope.assert_current(session)

    result = await create_resource(
        session,
        scope=scope,
        data=body,
        now=now,
    )
    response.headers["Location"] = f"/api/v1/admin/resources/{result.id}"
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.patch("/admin/resources/{id}", response_model=Resource)
async def update_resource_endpoint(
    id: uuid.UUID,
    body: ResourcePatch,
    response: Response,
    scope: AuthorizedScope = Depends(authorize(Policy.admin)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> Resource:
    """Guarded patch of a resource with optimistic locking (E19)."""
    now = datetime.now(timezone.utc)
    if scope.assert_current is not None:
        await scope.assert_current(session)

    result = await update_resource(
        session,
        scope=scope,
        data=body,
        now=now,
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


@router.delete("/admin/resources/{id}", response_model=Resource)
async def archive_resource_endpoint(
    id: uuid.UUID,
    response: Response,
    scope: AuthorizedScope = Depends(authorize(Policy.admin)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> Resource:
    """Soft archive a resource by setting active=false (E20)."""
    now = datetime.now(timezone.utc)
    if scope.assert_current is not None:
        await scope.assert_current(session)

    result = await archive_resource(
        session,
        scope=scope,
        data=EmptyBody(),
        now=now,
    )
    response.headers["ETag"] = f'"{result.version}"'
    return result


# --- Phase 2: Constraint-Backed Blackouts (E21–E23) ---


@router.get("/admin/resources/{id}/blackouts", response_model=Page[BookingSchema])
async def list_resource_blackouts_endpoint(
    id: uuid.UUID,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    scope: AuthorizedScope = Depends(authorize(Policy.admin)),
    session: AsyncSession = Depends(get_session),
) -> Page[BookingSchema]:
    """List blackouts for a specific resource, newest first (E21)."""
    return await list_resource_blackouts(
        session,
        scope=scope,
        resource_id=id,
        limit=limit,
        offset=offset,
    )


@router.post("/admin/resources/{id}/blackouts", status_code=201)
async def create_blackout_endpoint(
    request: Request,
    id: uuid.UUID,
    body: BlackoutCreate,
    idempotency_key_header: str | None = Header(None, alias="Idempotency-Key"),
    scope: AuthorizedScope = Depends(authorize(Policy.admin)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> Response:
    """Create an idempotent constraint-backed blackout (E22)."""
    key = _parse_idempotency_key(idempotency_key_header)
    now = datetime.now(timezone.utc)

    if scope.assert_current is not None:
        await scope.assert_current(session)

    async def operation() -> StoredResponse:
        db_clock = await session.scalar(select(func.clock_timestamp()))
        db_now = db_clock if db_clock is not None else now

        try:
            booking = await create_blackout(
                session,
                scope=scope,
                data=body,
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

        booking_dict = booking.model_dump(mode="json")
        headers = {
            "Location": f"/api/v1/admin/blackouts/{booking.id}",
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


@router.delete("/admin/blackouts/{id}", response_model=BookingSchema)
async def cancel_blackout_endpoint(
    id: uuid.UUID,
    response: Response,
    scope: AuthorizedScope = Depends(authorize(Policy.admin)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> BookingSchema:
    """Cancel a blackout and promote waiters atomically (E23)."""
    now = datetime.now(timezone.utc)
    if scope.assert_current is not None:
        await scope.assert_current(session)

    booking = await cancel_blackout(
        session,
        scope=scope,
        data=EmptyBody(),
        now=now,
    )
    response.headers["ETag"] = f'"{booking.version}"'
    return booking


# --- Phase 3: Administrator Booking Operations (E24–E26) ---


@router.get("/admin/bookings", response_model=Page[BookingSchema])
async def list_admin_bookings_endpoint(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    resource_id: uuid.UUID | None = Query(default=None),
    user_id: uuid.UUID | None = Query(default=None),
    status: BookingStatusFilter | None = Query(default=None),
    starts_at: datetime | None = Query(default=None),
    ends_at: datetime | None = Query(default=None),
    scope: AuthorizedScope = Depends(authorize(Policy.admin)),
    session: AsyncSession = Depends(get_session),
) -> Page[BookingSchema]:
    """List reservations across the system with optional filters (E24)."""
    return await list_admin_bookings(
        session,
        scope=scope,
        limit=limit,
        offset=offset,
        resource_id=resource_id,
        user_id=user_id,
        status=status,
        starts_at=starts_at,
        ends_at=ends_at,
    )


@router.get("/admin/bookings/{id}", response_model=BookingSchema)
async def get_admin_booking_endpoint(
    id: uuid.UUID,
    response: Response,
    scope: AuthorizedScope = Depends(authorize(Policy.admin)),
    session: AsyncSession = Depends(get_session),
) -> BookingSchema:
    """Retrieve details for any reservation across the system (E25)."""
    booking = await get_admin_booking(session, scope=scope, booking_id=id)
    response.headers["ETag"] = f'"{booking.version}"'
    return booking


@router.post("/admin/bookings/{id}/cancel", response_model=BookingSchema)
async def cancel_admin_booking_endpoint(
    id: uuid.UUID,
    body: Cancel,
    response: Response,
    scope: AuthorizedScope = Depends(authorize(Policy.admin)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> BookingSchema:
    """Cancel any active or running reservation as an administrator (E26)."""
    if scope.expected_version is None:
        raise RuntimeError("Expected version must be present in AuthorizedScope for cancel")

    now = datetime.now(timezone.utc)
    if scope.assert_current is not None:
        await scope.assert_current(session)

    booking = await cancel_booking(
        session,
        scope=scope,
        booking_id=id,
        expected_version=scope.expected_version,
        reason=body.reason,
        now=now,
    )
    response.headers["ETag"] = f'"{booking.version}"'
    return booking


# --- Existing: Invitations (E29) ---


@router.post("/admin/invitations", response_model=InvitationResult, status_code=201)
async def create_invitation_endpoint(
    body: InviteCreate,
    response: Response,
    scope: AuthorizedScope = Depends(authorize(Policy.admin)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> InvitationResult:
    now = datetime.now(timezone.utc)
    if scope.assert_current is not None:
        await scope.assert_current(session)

    result = await create_invitation(
        session,
        scope=scope,
        data=body,
        now=now,
    )
    response.headers["Cache-Control"] = "no-store"
    return result
