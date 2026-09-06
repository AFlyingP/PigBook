import base64
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import InvalidCredentialsError
from app.auth.models import RefreshToken, User
from app.auth.passwords import normalize_email, verify_dummy_password, verify_password
from app.auth.schemas import TokenResponse
from app.auth.schemas import User as UserSchema
from app.config import get_settings


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
