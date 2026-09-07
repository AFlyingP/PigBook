import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import (
    AuthorizedScope,
    InvalidRefreshError,
    InvalidTokenError,
    OriginRejectedError,
    Policy,
    authorize,
)
from app.auth.models import User as UserModel
from app.auth.rate_limit import (
    RateLimitBucket,
    check_login_rate_limit,
    consume_rate_limits,
    get_client_ip,
    hash_identity,
)
from app.auth.schemas import Login, Register, TokenResponse, User
from app.auth.service import login, register, revoke_family, rotate_refresh
from app.config import get_settings
from app.db.session import get_session, transaction_dependency

router = APIRouter()


@router.post("/auth/register", response_model=User, status_code=201)
async def register_endpoint(
    body: Register,
    request: Request,
    _scope: AuthorizedScope = Depends(authorize(Policy.public)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> User:
    now = datetime.now(timezone.utc)
    # Rate limit: 5/IP/hour per Spec 8.2
    await _check_ip_rate_limit(request, scope="register:ip", limit=5, window_seconds=3600, now=now)
    req_id = (
        uuid.UUID(request.state.request_id)
        if hasattr(request.state, "request_id")
        else uuid.uuid4()
    )
    return await register(session, data=body, now=now, request_id=req_id)


@router.post("/auth/login", response_model=TokenResponse)
async def login_endpoint(
    body: Login,
    request: Request,
    response: Response,
    _scope: AuthorizedScope = Depends(authorize(Policy.public)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
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


def _enforce_exact_origin(request: Request) -> None:
    settings = get_settings()
    origin = request.headers.get("origin")
    if origin != settings.APP_ORIGIN:
        raise OriginRejectedError("Origin not allowed")


async def _check_ip_rate_limit(
    request: Request,
    scope: str,
    limit: int = 30,
    window_seconds: int = 60,
    now: datetime | None = None,
) -> None:
    if now is None:
        now = datetime.now(timezone.utc)

    client_ip = get_client_ip(request)
    ip_hash = hash_identity(client_ip)

    timestamp = int(now.timestamp())
    start_epoch = timestamp - (timestamp % window_seconds)
    window_start = datetime.fromtimestamp(start_epoch, tz=timezone.utc)

    bucket = RateLimitBucket(
        scope=scope,
        identity_hash=ip_hash,
        window_start=window_start,
        limit=limit,
        window_seconds=window_seconds,
    )
    await consume_rate_limits([bucket])


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh_endpoint(
    request: Request,
    response: Response,
    _scope: AuthorizedScope = Depends(authorize(Policy.public)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> TokenResponse:
    # 1. Enforce exact Origin FIRST
    _enforce_exact_origin(request)

    now = datetime.now(timezone.utc)

    # 2. Rate limit (30/IP/min) before domain transaction
    await _check_ip_rate_limit(request, scope="refresh:ip", limit=30, now=now)

    # 3. Read cookie by environment-dependent name
    settings = get_settings()
    is_secure = settings.APP_ENV not in ("local", "test")
    cookie_name = "__Secure-commonsbook_rt" if is_secure else "commonsbook_rt"

    raw_token = request.cookies.get(cookie_name)
    if not raw_token or not raw_token.strip():
        raise InvalidRefreshError("Missing or invalid refresh cookie")

    # 4. Rotate refresh token in domain transaction
    result = await rotate_refresh(session, raw_token=raw_token.strip(), now=now)

    # 5. Set rotated cookie with identical attributes to login
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


@router.post("/auth/logout", status_code=204, response_class=Response)
async def logout_endpoint(
    request: Request,
    response: Response,
    _scope: AuthorizedScope = Depends(authorize(Policy.public)),
    session: AsyncSession = Depends(transaction_dependency, scope="function"),
) -> Response:
    # 1. Enforce exact Origin FIRST (even when cookie is absent)
    _enforce_exact_origin(request)

    now = datetime.now(timezone.utc)

    # 2. Rate limit (30/IP/min) before domain transaction
    await _check_ip_rate_limit(request, scope="logout:ip", limit=30, now=now)

    settings = get_settings()
    is_secure = settings.APP_ENV not in ("local", "test")
    cookie_name = "__Secure-commonsbook_rt" if is_secure else "commonsbook_rt"

    # 3. Revoke family if cookie present
    raw_token = request.cookies.get(cookie_name)
    if raw_token and raw_token.strip():
        await revoke_family(session, raw_token=raw_token.strip(), now=now)

    # 4. Clear cookie
    response.delete_cookie(
        key=cookie_name,
        path="/api/v1/auth",
        httponly=True,
        secure=is_secure,
        samesite="lax",
    )
    return Response(status_code=204, headers=dict(response.headers))
