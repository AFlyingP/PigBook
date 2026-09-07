import base64
import hashlib
import os
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.audit import append_audit_log
from app.admin.schemas import InviteCreate
from app.auth.dependencies import AuthorizedScope
from app.auth.models import Invitation, User
from app.auth.passwords import normalize_email
from app.auth.schemas import InvitationResult
from app.auth.service import EmailExistsError
from app.config import get_settings


async def create_invitation(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    data: InviteCreate,
    now: datetime,
    request_id: uuid.UUID | None = None,
) -> InvitationResult:
    """Create a single-use invitation for registration (E29).

    The raw token is generated here, returned once in the InvitationResult,
    and only its SHA-256 hash is persisted in the database.
    """
    norm_email = normalize_email(data.email)

    # Check whether a user with this email already exists
    stmt = select(User.id).where(User.email == norm_email)
    res = await session.execute(stmt)
    if res.scalar_one_or_none() is not None:
        raise EmailExistsError("A user with this email already exists")

    raw_bytes = os.urandom(32)
    raw_token = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    expires_at = now + timedelta(days=7)
    invitation_id = uuid.uuid4()
    invitation = Invitation(
        id=invitation_id,
        email=norm_email,
        token_hash=token_hash,
        role=data.role,
        created_by=scope.principal_id,
        created_at=now,
        expires_at=expires_at,
        consumed_at=None,
    )
    session.add(invitation)

    settings = get_settings()
    invitation_url = f"{settings.APP_ORIGIN}/register#token={raw_token}"

    req_id = request_id or uuid.uuid4()
    await append_audit_log(
        session,
        action="admin.invitation_create",
        target_type="invitation",
        target_id=invitation_id,
        actor_id=scope.principal_id,
        request_id=req_id,
        details={
            "email": norm_email,
            "role": data.role,
            "expires_at": expires_at.isoformat(),
        },
        now=now,
    )

    await session.flush()

    return InvitationResult(
        id=invitation_id,
        email=norm_email,
        role=data.role,
        expires_at=expires_at,
        invitation_url=invitation_url,
    )
