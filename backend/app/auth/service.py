import base64
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.audit import append_audit_log
from app.auth.dependencies import InvalidCredentialsError, InvalidRefreshError
from app.auth.models import Invitation, RefreshToken, User
from app.auth.passwords import (
    hash_password,
    normalize_email,
    verify_dummy_password,
    verify_password,
)
from app.auth.schemas import Register, TokenResponse
from app.auth.schemas import User as UserSchema
from app.config import get_settings
from app.db.session import get_sessionmaker


class InvalidInvitationError(Exception):
    def __init__(self, message: str = "Invalid or expired invitation") -> None:
        self.message = message
        super().__init__(message)


class EmailExistsError(Exception):
    def __init__(self, message: str = "A user with this email already exists") -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class TokenResult:
    response: TokenResponse
    raw_refresh: str


async def login(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    now: datetime,
) -> TokenResult:
    """Authenticate user with email and password, issuing access JWT and refresh family row."""
    try:
        norm_email = normalize_email(email)
    except ValueError:
        verify_dummy_password(password)
        raise InvalidCredentialsError("Invalid email or password")

    stmt = select(User).where(User.email == norm_email)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()

    if user is None:
        verify_dummy_password(password)
        raise InvalidCredentialsError("Invalid email or password")

    stored_hash = str(user.password_hash)
    if not verify_password(password, stored_hash):
        raise InvalidCredentialsError("Invalid email or password")

    verified_hash = stored_hash

    # Acquire user FOR SHARE and recheck enabled state and password-hash equality
    share_stmt = select(User).where(User.id == user.id).with_for_update(read=True)
    share_res = await session.execute(share_stmt)
    locked_user = share_res.scalar_one_or_none()

    if locked_user is None or not locked_user.enabled or locked_user.password_hash != verified_hash:
        raise InvalidCredentialsError("Invalid email or password")

    settings = get_settings()
    jwt_secret = settings.JWT_SECRET
    if not jwt_secret or len(jwt_secret.encode("utf-8")) < 32:
        raise RuntimeError("JWT_SECRET must be configured and at least 32 bytes")

    ttl = settings.ACCESS_TOKEN_TTL_SECONDS
    exp = now + timedelta(seconds=ttl)
    claims = {
        "sub": str(locked_user.id),
        "role": str(locked_user.role),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": str(uuid.uuid4()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
    }
    access_token = jwt.encode(claims, jwt_secret, algorithm="HS256")

    raw_bytes = os.urandom(32)
    raw_refresh = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
    token_hash = hashlib.sha256(raw_refresh.encode("utf-8")).hexdigest()

    family_id = uuid.uuid4()
    refresh_record = RefreshToken(
        id=uuid.uuid4(),
        user_id=locked_user.id,
        token_hash=token_hash,
        family_id=family_id,
        parent_id=None,
        created_at=now,
        expires_at=now + timedelta(days=7),
        family_expires_at=now + timedelta(days=30),
        used_at=None,
        revoked_at=None,
    )
    session.add(refresh_record)
    await session.flush()

    user_response = UserSchema.model_validate(locked_user)
    token_response = TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=ttl,
        user=user_response,
    )

    return TokenResult(
        response=token_response,
        raw_refresh=raw_refresh,
    )


def _derive_family_advisory_key(family_id: uuid.UUID) -> int:
    family_uuid_str = str(family_id).lower()
    family_digest = hashlib.sha256(
        f"commonsbook-refresh:{family_uuid_str}".encode("utf-8")
    ).digest()
    return int.from_bytes(family_digest[:8], "big", signed=True)


async def rotate_refresh(
    session: AsyncSession,
    *,
    raw_token: str,
    now: datetime,
) -> TokenResult:
    """Rotate a refresh token following the Spec 8.1 locking order."""
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    # 1. Resolve token hash to immutable user/family IDs with NO row lock
    resolve_stmt = select(RefreshToken.user_id, RefreshToken.family_id).where(
        RefreshToken.token_hash == token_hash
    )
    resolve_res = await session.execute(resolve_stmt)
    resolved = resolve_res.one_or_none()
    if resolved is None:
        raise InvalidRefreshError("Invalid or expired refresh token")
    user_id, family_id = resolved

    # 2. Acquire user row FOR SHARE; reject if missing or disabled
    user_stmt = select(User).where(User.id == user_id).with_for_update(read=True)
    user_res = await session.execute(user_stmt)
    locked_user = user_res.scalar_one_or_none()
    if locked_user is None or not locked_user.enabled:
        raise InvalidRefreshError("Invalid or expired refresh token")

    # 3. Acquire transaction-scoped advisory family lock
    advisory_key = _derive_family_advisory_key(family_id)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": advisory_key},
    )

    # 4. Acquire token row FOR UPDATE
    token_stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
    token_res = await session.execute(token_stmt)
    token = token_res.scalar_one_or_none()

    # 5. Re-read and validate all token fields after locks
    if token is None or token.user_id != locked_user.id or token.family_id != family_id:
        raise InvalidRefreshError("Invalid or expired refresh token")

    # 6. Reject if revoked, or if expired
    if token.revoked_at is not None or token.expires_at <= now or token.family_expires_at <= now:
        raise InvalidRefreshError("Invalid or expired refresh token")

    # 7. Reuse detection: if used_at is not None, revoke entire family independently and raise
    if token.used_at is not None:
        await session.rollback()
        sessionmaker = get_sessionmaker()
        reuse_req_id = uuid.uuid4()
        async with sessionmaker() as independent_session:
            async with independent_session.begin():
                await revoke_family(independent_session, raw_token=raw_token, now=now)
                await append_audit_log(
                    independent_session,
                    action="auth.refresh_reuse",
                    target_type="refresh_family",
                    target_id=family_id,
                    actor_id=user_id,
                    request_id=reuse_req_id,
                    details={
                        "user_id": str(user_id),
                        "family_id": str(family_id),
                        "event_category": "refresh_reuse",
                    },
                    now=now,
                )
        raise InvalidRefreshError("Invalid or expired refresh token")

    # 8. Mark token used and create exactly one child row atomically
    token.used_at = now

    raw_bytes = os.urandom(32)
    child_raw_refresh = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
    child_token_hash = hashlib.sha256(child_raw_refresh.encode("utf-8")).hexdigest()

    child_expires_at = min(now + timedelta(days=7), token.family_expires_at)
    child_record = RefreshToken(
        id=uuid.uuid4(),
        user_id=locked_user.id,
        token_hash=child_token_hash,
        family_id=token.family_id,
        parent_id=token.id,
        created_at=now,
        expires_at=child_expires_at,
        family_expires_at=token.family_expires_at,
        used_at=None,
        revoked_at=None,
    )
    session.add(child_record)
    await session.flush()

    # 9. Mint fresh access JWT (15 min)
    settings = get_settings()
    jwt_secret = settings.JWT_SECRET
    if not jwt_secret or len(jwt_secret.encode("utf-8")) < 32:
        raise RuntimeError("JWT_SECRET must be configured and at least 32 bytes")

    ttl = settings.ACCESS_TOKEN_TTL_SECONDS
    exp = now + timedelta(seconds=ttl)
    claims = {
        "sub": str(locked_user.id),
        "role": str(locked_user.role),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": str(uuid.uuid4()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
    }
    access_token = jwt.encode(claims, jwt_secret, algorithm="HS256")

    user_response = UserSchema.model_validate(locked_user)
    token_response = TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=ttl,
        user=user_response,
    )

    return TokenResult(
        response=token_response,
        raw_refresh=child_raw_refresh,
    )


async def revoke_family(
    session: AsyncSession,
    *,
    raw_token: str,
    now: datetime,
) -> None:
    """Revoke all tokens in a refresh family following the Spec 8.1 locking order."""
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    # 1. Resolve token hash with NO row lock
    resolve_stmt = select(RefreshToken.user_id, RefreshToken.family_id).where(
        RefreshToken.token_hash == token_hash
    )
    resolve_res = await session.execute(resolve_stmt)
    resolved = resolve_res.one_or_none()
    if resolved is None:
        return
    user_id, family_id = resolved

    # 2. Acquire user row FOR SHARE
    user_stmt = select(User).where(User.id == user_id).with_for_update(read=True)
    user_res = await session.execute(user_stmt)
    user = user_res.scalar_one_or_none()
    if user is None:
        return

    # 3. Acquire transaction-scoped advisory family lock
    advisory_key = _derive_family_advisory_key(family_id)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": advisory_key},
    )

    # 4. Acquire token row FOR UPDATE
    token_stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
    token_res = await session.execute(token_stmt)
    token = token_res.scalar_one_or_none()
    if token is None:
        return

    # 5. Revoke entire family
    rev_stmt = (
        update(RefreshToken)
        .where(
            RefreshToken.family_id == family_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    await session.execute(rev_stmt)
    await session.flush()


async def register(
    session: AsyncSession,
    *,
    data: Register,
    now: datetime,
    request_id: uuid.UUID | None = None,
) -> UserSchema:
    """Register a new user from a valid, single-use invitation (E01)."""
    token_hash = hashlib.sha256(data.invitation_token.encode("utf-8")).hexdigest()
    try:
        norm_email = normalize_email(data.email)
    except ValueError:
        raise InvalidInvitationError("Invalid or expired invitation")

    # Lock the invitation row for update to serialize concurrent registrations
    stmt = select(Invitation).where(Invitation.token_hash == token_hash).with_for_update()
    res = await session.execute(stmt)
    invitation = res.scalar_one_or_none()

    if invitation is None:
        raise InvalidInvitationError("Invalid or expired invitation")

    if invitation.consumed_at is not None:
        raise InvalidInvitationError("Invalid or expired invitation")

    if invitation.expires_at <= now:
        raise InvalidInvitationError("Invalid or expired invitation")

    if invitation.email.strip().lower() != norm_email:
        raise InvalidInvitationError("Invalid or expired invitation")

    # Check if a user with this email already exists
    user_stmt = select(User.id).where(User.email == norm_email)
    user_res = await session.execute(user_stmt)
    if user_res.scalar_one_or_none() is not None:
        raise EmailExistsError("A user with this email already exists")

    # The role comes ONLY from the invitation row
    user_role = invitation.role
    pwd_hash = hash_password(data.password)

    new_user = User(
        id=uuid.uuid4(),
        email=norm_email,
        password_hash=pwd_hash,
        display_name=data.display_name.strip(),
        role=user_role,
        enabled=True,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(new_user)

    # Mark invitation consumed atomically
    invitation.consumed_at = now

    try:
        await session.flush()
    except IntegrityError as exc:
        exc_str = str(exc).lower()
        if "users_email" in exc_str or "unique constraint" in exc_str:
            raise EmailExistsError("A user with this email already exists") from exc
        raise InvalidInvitationError("Invalid or expired invitation") from exc

    req_id = request_id or uuid.uuid4()
    await append_audit_log(
        session,
        action="auth.register",
        target_type="user",
        target_id=new_user.id,
        actor_id=new_user.id,
        request_id=req_id,
        details={
            "email": norm_email,
            "role": user_role,
            "invitation_id": str(invitation.id),
        },
        now=now,
    )

    return UserSchema.model_validate(new_user)
