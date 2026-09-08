import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthorizedScope, AuthRequiredError, Policy, authorize
from app.auth.rate_limit import check_mutation_rate_limit
from app.db.session import get_session, transaction_dependency
from app.resources.schemas import Page
from app.waitlist.schemas import (
    AcceptOffer,
    WaitCreate,
    WaitEntry,
    WaitlistStatusFilter,
)
from app.waitlist.service import (
    accept_offer,
    decline_entry,
    join_waitlist,
    list_own_waitlist,
)

router = APIRouter()


@router.post("/waitlist", status_code=201, response_model=WaitEntry)
async def join_waitlist_endpoint(
    request: Request,
    body: WaitCreate,
    scope: AuthorizedScope = Depends(authorize(Policy.authenticated)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> Response:
    """Join the waitlist for a resource window (E13).

    Consumes mutation rate budget before taking inventory locks.
    """
    if scope.principal_id is None:
        raise AuthRequiredError("Authentication required")

    now = datetime.now(timezone.utc)
    await check_mutation_rate_limit(scope.principal_id, now)

    if scope.assert_current is not None:
        await scope.assert_current(session)

    db_clock = await session.scalar(select(func.clock_timestamp()))
    db_now = db_clock if db_clock is not None else now

    entry = await join_waitlist(session, scope=scope, data=body, now=db_now)

    return JSONResponse(
        status_code=201,
        content=entry.model_dump(mode="json"),
        headers={
            "Location": f"/api/v1/waitlist/{entry.id}",
            "ETag": f'"{entry.version}"',
        },
    )


@router.get("/waitlist", response_model=Page[WaitEntry])
async def list_waitlist_endpoint(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    status: WaitlistStatusFilter | None = Query(default=None),
    scope: AuthorizedScope = Depends(authorize(Policy.own_waitlist)),
    session: AsyncSession = Depends(get_session),
) -> Page[WaitEntry]:
    """List the caller's own waitlist entries (E14)."""
    if scope.principal_id is None:
        raise AuthRequiredError("Authentication required")

    return await list_own_waitlist(
        session,
        scope=scope,
        limit=limit,
        offset=offset,
        status=status.value if status else None,
    )


@router.delete("/waitlist/{id}", response_model=WaitEntry)
async def delete_waitlist_endpoint(
    id: uuid.UUID,
    scope: AuthorizedScope = Depends(authorize(Policy.own_waitlist)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> Response:
    """Decline or withdraw a waitlist entry (E15)."""
    if scope.principal_id is None:
        raise AuthRequiredError("Authentication required")
    if scope.object_id is None or scope.expected_version is None:
        raise RuntimeError("own_waitlist scope for decline is missing object identity")

    now = datetime.now(timezone.utc)
    if scope.assert_current is not None:
        await scope.assert_current(session)

    entry = await decline_entry(
        session,
        scope=scope,
        entry_id=scope.object_id,
        expected_version=scope.expected_version,
        now=now,
    )

    return JSONResponse(
        status_code=200,
        content=entry.model_dump(mode="json"),
        headers={"ETag": f'"{entry.version}"'},
    )


@router.post("/waitlist/{id}/accept", response_model=None)
async def accept_waitlist_endpoint(
    id: uuid.UUID,
    body: AcceptOffer,
    scope: AuthorizedScope = Depends(authorize(Policy.own_waitlist)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> Response:
    """Accept an offered waitlist entry (E16)."""
    if scope.principal_id is None:
        raise AuthRequiredError("Authentication required")
    if scope.object_id is None or scope.expected_version is None:
        raise RuntimeError("own_waitlist scope for accept is missing object identity")

    now = datetime.now(timezone.utc)
    if scope.assert_current is not None:
        await scope.assert_current(session)

    stored = await accept_offer(
        session,
        scope=scope,
        entry_id=scope.object_id,
        expected_version=scope.expected_version,
        now=now,
    )

    return JSONResponse(
        status_code=stored.status,
        content=stored.body,
        headers=stored.headers,
    )
