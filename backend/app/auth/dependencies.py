import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import jwt
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.config import get_settings
from app.db.session import get_session


class AuthRequiredError(Exception):
    def __init__(self, message: str = "Authentication required") -> None:
        self.message = message
        super().__init__(message)


class InvalidTokenError(Exception):
    def __init__(self, message: str = "Invalid or expired token") -> None:
        self.message = message
        super().__init__(message)


class ForbiddenError(Exception):
    def __init__(self, message: str = "Insufficient permissions") -> None:
        self.message = message
        super().__init__(message)


class InvalidCredentialsError(Exception):
    def __init__(self, message: str = "Invalid email or password") -> None:
        self.message = message
        super().__init__(message)


class InvalidRefreshError(Exception):
    def __init__(self, message: str = "Invalid or expired refresh token") -> None:
        self.message = message
        super().__init__(message)


class OriginRejectedError(Exception):
    def __init__(self, message: str = "Origin not allowed") -> None:
        self.message = message
        super().__init__(message)


class Policy(str, Enum):
    public = "public"
    authenticated = "authenticated"
    own_booking = "own_booking"
    own_waitlist = "own_waitlist"
    admin = "admin"
    metrics = "metrics"


@dataclass(frozen=True)
class AuthorizedScope:
    principal_id: uuid.UUID | None
    policy: Policy
    object_id: uuid.UUID | None = None
    resource_id: uuid.UUID | None = None
    expected_version: int | None = None


policy_registry: dict[str, Policy] = {
    "E02": Policy.public,
    "E03": Policy.public,
    "E04": Policy.public,
    "E05": Policy.authenticated,
    "E35": Policy.public,
}


def authorize(
    policy: Policy,
) -> Callable[[Request, AsyncSession], Coroutine[Any, Any, AuthorizedScope]]:
    """FastAPI authorization dependency callable enforcing centralized policy decisions."""

    async def dependency(
        request: Request,
        session: AsyncSession = Depends(get_session),
    ) -> AuthorizedScope:
        if policy == Policy.public:
            return AuthorizedScope(principal_id=None, policy=policy)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise AuthRequiredError("Authentication required")

        token = auth_header[7:].strip()
        if not token:
            raise AuthRequiredError("Authentication required")

        settings = get_settings()
        jwt_secret = settings.JWT_SECRET
        if not jwt_secret or len(jwt_secret.encode("utf-8")) < 32:
            raise RuntimeError("JWT_SECRET must be configured and at least 32 bytes")

        try:
            payload = jwt.decode(
                token,
                jwt_secret,
                algorithms=["HS256"],
                issuer=settings.JWT_ISSUER,
                audience=settings.JWT_AUDIENCE,
                options={
                    "require": ["sub", "iat", "exp", "jti"],
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except Exception as exc:
            raise InvalidTokenError("Invalid or expired token") from exc

        sub = payload.get("sub")
        if not sub:
            raise InvalidTokenError("Invalid or expired token")

        try:
            user_id = uuid.UUID(str(sub))
        except (ValueError, TypeError):
            raise InvalidTokenError("Invalid or expired token")

        # Reload user enabled status and role from database on EVERY request.
        # Role inside JWT token is NOT trusted.
        stmt = select(User.id, User.role, User.enabled).where(User.id == user_id)
        result = await session.execute(stmt)
        user_row = result.one_or_none()

        if user_row is None:
            raise InvalidTokenError("Invalid or expired token")

        db_id, db_role, db_enabled = user_row
        if not db_enabled:
            raise InvalidTokenError("Invalid or expired token")

        if policy == Policy.admin:
            if db_role != "admin":
                raise ForbiddenError("Insufficient permissions")
        elif policy == Policy.authenticated:
            if db_role not in ("member", "admin"):
                raise ForbiddenError("Insufficient permissions")

        return AuthorizedScope(
            principal_id=db_id,
            policy=policy,
        )

    return dependency
