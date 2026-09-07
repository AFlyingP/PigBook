import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.schemas import InviteCreate
from app.admin.service import create_invitation
from app.auth.dependencies import AuthorizedScope, Policy, authorize
from app.auth.schemas import InvitationResult
from app.db.session import transaction_dependency

router = APIRouter()


@router.post("/admin/invitations", response_model=InvitationResult, status_code=201)
async def create_invitation_endpoint(
    body: InviteCreate,
    request: Request,
    response: Response,
    scope: AuthorizedScope = Depends(authorize(Policy.admin)),
    session: AsyncSession = Depends(transaction_dependency),
) -> InvitationResult:
    now = datetime.now(timezone.utc)
    req_id = (
        uuid.UUID(request.state.request_id)
        if hasattr(request.state, "request_id")
        else uuid.uuid4()
    )
    result = await create_invitation(
        session,
        scope=scope,
        data=body,
        now=now,
        request_id=req_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return result
