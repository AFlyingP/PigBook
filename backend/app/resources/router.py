import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthorizedScope, Policy, authorize
from app.db.session import get_session
from app.resources.schemas import Availability, Page, Resource
from app.resources.service import (
    _get_active_resource,
    availability,
    list_resources,
    validate_window,
)

router = APIRouter()


@router.get("/resources", response_model=Page[Resource])
async def list_resources_endpoint(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    scope: AuthorizedScope = Depends(authorize(Policy.authenticated)),
    session: AsyncSession = Depends(get_session),
) -> Page[Resource]:
    """List active resources with pagination."""
    return await list_resources(session, scope=scope, limit=limit, offset=offset)


@router.get("/resources/{id}", response_model=Resource)
async def get_resource_endpoint(
    id: uuid.UUID,
    response: Response,
    _scope: AuthorizedScope = Depends(authorize(Policy.authenticated)),
    session: AsyncSession = Depends(get_session),
) -> Resource:
    """Retrieve an active resource by id, emitting an ETag header."""
    resource = await _get_active_resource(session, id)
    response.headers["ETag"] = f'"{resource.version}"'
    return Resource.model_validate(resource)


@router.get("/resources/{id}/availability", response_model=Availability)
async def get_resource_availability_endpoint(
    id: uuid.UUID,
    starts_at: str = Query(...),
    ends_at: str = Query(...),
    scope: AuthorizedScope = Depends(authorize(Policy.authenticated)),
    session: AsyncSession = Depends(get_session),
) -> Availability:
    """Query occupied booking intervals for an active resource within the specified window."""
    s_utc, e_utc = validate_window(starts_at, ends_at)
    return await availability(
        session,
        scope=scope,
        resource_id=id,
        starts_at=s_utc,
        ends_at=e_utc,
    )
