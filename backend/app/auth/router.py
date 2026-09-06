from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import (
    AuthorizedScope,
    InvalidTokenError,
    Policy,
    authorize,
)
from app.auth.models import User as UserModel
from app.auth.rate_limit import check_login_rate_limit
from app.auth.schemas import Login, TokenResponse, User
from app.auth.service import login
from app.config import get_settings
from app.db.session import get_session, transaction_dependency

router = APIRouter()


@router.post("/auth/login", response_model=TokenResponse)
async def login_endpoint(
    body: Login,
    request: Request,
    response: Response,
    _scope: AuthorizedScope = Depends(authorize(Policy.public)),
    session: AsyncSession = Depends(transaction_dependency),
) -> TokenResponse:
    now = datetime.now(timezone.utc)
    # Rate limit check runs and commits in its own short independent transaction
    await check_login_rate_limit(request, body.email, now=now)

    result = await login(session, email=body.email, password=body.password, now=now)

    settings = get_settings()
    is_secure = settings.APP_ENV not in ("local", "test")
    cookie_name = "__Secure-commonsbook_rt" if is_secure else "commonsbook_rt"

    response.set_cookie(
        key=cookie_name,
        value=result.raw_refresh,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        path="/api/v1/auth",
        max_age=30 * 24 * 3600,
    )
    response.headers["Cache-Control"] = "no-store"

    return result.response


@router.get("/me", response_model=User)
async def me_endpoint(
    scope: AuthorizedScope = Depends(authorize(Policy.authenticated)),
    session: AsyncSession = Depends(get_session),
) -> User:
    stmt = select(UserModel).where(UserModel.id == scope.principal_id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()
    if user is None or not user.enabled:
        raise InvalidTokenError("Invalid or expired token")
    return User.model_validate(user)
