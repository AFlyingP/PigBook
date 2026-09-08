import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

from app.admin.router import router as admin_router
from app.auth.dependencies import (
    AuthRequiredError,
    ForbiddenError,
    InvalidCredentialsError,
    InvalidIfMatchError,
    InvalidRefreshError,
    InvalidTokenError,
    ObjectNotFoundError,
    OriginRejectedError,
    PreconditionRequiredError,
)
from app.auth.rate_limit import RateLimitExceeded
from app.auth.router import router as auth_router
from app.auth.service import EmailExistsError, InvalidInvitationError
from app.bookings.idempotency import (
    IdempotencyKeyInvalid,
    IdempotencyKeyMismatch,
    IdempotencyKeyRequired,
    IncompleteIdempotencyRecord,
)
from app.bookings.router import router as bookings_router
from app.bookings.service import (
    InvalidState,
    TooLate,
    VersionMismatch,
)
from app.bookings.service import (
    InvalidWindowError as BookingInvalidWindowError,
)
from app.bookings.service import (
    NotFoundError as BookingNotFoundError,
)
from app.config import get_settings
from app.resources.router import router as resources_router
from app.resources.service import InvalidWindowError, NotFoundError


def make_error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    content = {
        "error": {
            "code": code,
            "message": message,
            "details": details if details is not None else {},
        }
    }
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def is_valid_uuid(val: str | None) -> bool:
    if not val or len(val) != 36:
        return False
    try:
        parsed = uuid.UUID(val)
        return str(parsed) == val.lower()
    except (ValueError, TypeError, AttributeError):
        return False


def create_app() -> FastAPI:
    settings = get_settings()
    is_docs_enabled = settings.APP_ENV in ("local", "test")

    app = FastAPI(
        title="CommonsBook",
        version=settings.RELEASE_SHA,
        openapi_url="/openapi.json" if is_docs_enabled else None,
        docs_url="/docs" if is_docs_enabled else None,
        redoc_url="/redoc" if is_docs_enabled else None,
    )

    # CORS: exact APP_ORIGIN only, no wildcard
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.APP_ORIGIN],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "If-Match",
            "X-Request-ID",
        ],
        expose_headers=[
            "ETag",
            "Location",
            "X-Request-ID",
            "Idempotency-Replayed",
            "Retry-After",
        ],
    )

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        inbound_id = request.headers.get("X-Request-ID")
        if request.url.path.startswith("/api/v1"):
            if inbound_id and is_valid_uuid(inbound_id):
                request_id = str(uuid.UUID(inbound_id))
            else:
                request_id = str(uuid.uuid4())
        else:
            request_id = inbound_id if inbound_id else str(uuid.uuid4())
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    # Exception Handlers mapping to Spec 4.1 Error Envelope

    @app.exception_handler(AuthRequiredError)
    async def auth_required_handler(_request: Request, exc: AuthRequiredError) -> JSONResponse:
        return make_error_response(
            status_code=401,
            code="AUTH_REQUIRED",
            message=exc.message,
        )

    @app.exception_handler(InvalidTokenError)
    async def invalid_token_handler(_request: Request, exc: InvalidTokenError) -> JSONResponse:
        return make_error_response(
            status_code=401,
            code="INVALID_TOKEN",
            message=exc.message,
        )

    @app.exception_handler(InvalidCredentialsError)
    async def invalid_credentials_handler(
        _request: Request, exc: InvalidCredentialsError
    ) -> JSONResponse:
        return make_error_response(
            status_code=401,
            code="INVALID_CREDENTIALS",
            message=exc.message,
        )

    @app.exception_handler(ForbiddenError)
    async def forbidden_handler(_request: Request, exc: ForbiddenError) -> JSONResponse:
        return make_error_response(
            status_code=403,
            code="FORBIDDEN",
            message=exc.message,
        )

    @app.exception_handler(InvalidRefreshError)
    async def invalid_refresh_handler(_request: Request, exc: InvalidRefreshError) -> JSONResponse:
        return make_error_response(
            status_code=401,
            code="INVALID_REFRESH",
            message=exc.message,
        )

    @app.exception_handler(OriginRejectedError)
    async def origin_rejected_handler(_request: Request, exc: OriginRejectedError) -> JSONResponse:
        return make_error_response(
            status_code=403,
            code="ORIGIN_REJECTED",
            message=exc.message,
        )

    @app.exception_handler(InvalidInvitationError)
    async def invalid_invitation_handler(
        _request: Request, exc: InvalidInvitationError
    ) -> JSONResponse:
        return make_error_response(
            status_code=422,
            code="INVALID_INVITATION",
            message=exc.message,
        )

    @app.exception_handler(EmailExistsError)
    async def email_exists_handler(_request: Request, exc: EmailExistsError) -> JSONResponse:
        return make_error_response(
            status_code=409,
            code="EMAIL_EXISTS",
            message=exc.message,
        )

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(_request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return make_error_response(
            status_code=429,
            code="RATE_LIMITED",
            message=f"Rate limit exceeded. Retry after {exc.retry_after} seconds.",
            details={"retry_after": exc.retry_after},
            headers={"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_request: Request, exc: NotFoundError) -> JSONResponse:
        return make_error_response(
            status_code=404,
            code="NOT_FOUND",
            message=exc.message,
        )

    @app.exception_handler(InvalidWindowError)
    async def invalid_window_handler(_request: Request, exc: InvalidWindowError) -> JSONResponse:
        return make_error_response(
            status_code=422,
            code="INVALID_WINDOW",
            message=exc.message,
        )

    @app.exception_handler(BookingNotFoundError)
    async def booking_not_found_handler(
        _request: Request, exc: BookingNotFoundError
    ) -> JSONResponse:
        return make_error_response(
            status_code=404,
            code="NOT_FOUND",
            message=exc.message,
        )

    @app.exception_handler(BookingInvalidWindowError)
    async def booking_invalid_window_handler(
        _request: Request, exc: BookingInvalidWindowError
    ) -> JSONResponse:
        return make_error_response(
            status_code=422,
            code="INVALID_WINDOW",
            message=exc.message,
        )

    @app.exception_handler(ObjectNotFoundError)
    async def object_not_found_handler(_request: Request, exc: ObjectNotFoundError) -> JSONResponse:
        return make_error_response(
            status_code=404,
            code="NOT_FOUND",
            message=exc.message,
        )

    @app.exception_handler(PreconditionRequiredError)
    async def precondition_required_handler(
        _request: Request, exc: PreconditionRequiredError
    ) -> JSONResponse:
        return make_error_response(
            status_code=428,
            code="PRECONDITION_REQUIRED",
            message=exc.message,
        )

    @app.exception_handler(InvalidIfMatchError)
    async def invalid_if_match_handler(_request: Request, exc: InvalidIfMatchError) -> JSONResponse:
        return make_error_response(
            status_code=422,
            code="VALIDATION_ERROR",
            message=exc.message,
        )

    @app.exception_handler(VersionMismatch)
    async def version_mismatch_handler(_request: Request, exc: VersionMismatch) -> JSONResponse:
        return make_error_response(
            status_code=412,
            code="VERSION_MISMATCH",
            message=exc.message,
        )

    @app.exception_handler(TooLate)
    async def too_late_handler(_request: Request, exc: TooLate) -> JSONResponse:
        return make_error_response(
            status_code=409,
            code="TOO_LATE",
            message=exc.message,
        )

    @app.exception_handler(InvalidState)
    async def invalid_state_handler(_request: Request, exc: InvalidState) -> JSONResponse:
        return make_error_response(
            status_code=409,
            code="INVALID_STATE",
            message=exc.message,
        )

    @app.exception_handler(IdempotencyKeyRequired)
    async def idempotency_key_required_handler(
        _request: Request, exc: IdempotencyKeyRequired
    ) -> JSONResponse:
        return make_error_response(
            status_code=422,
            code="IDEMPOTENCY_KEY_REQUIRED",
            message=exc.message,
        )

    @app.exception_handler(IdempotencyKeyInvalid)
    async def idempotency_key_invalid_handler(
        _request: Request, exc: IdempotencyKeyInvalid
    ) -> JSONResponse:
        return make_error_response(
            status_code=422,
            code="IDEMPOTENCY_KEY_INVALID",
            message=exc.message,
        )

    @app.exception_handler(IdempotencyKeyMismatch)
    async def idempotency_key_mismatch_handler(
        _request: Request, exc: IdempotencyKeyMismatch
    ) -> JSONResponse:
        return make_error_response(
            status_code=422,
            code="IDEMPOTENCY_KEY_MISMATCH",
            message=exc.message,
        )

    @app.exception_handler(IncompleteIdempotencyRecord)
    async def incomplete_idempotency_record_handler(
        _request: Request, exc: IncompleteIdempotencyRecord
    ) -> JSONResponse:
        return make_error_response(
            status_code=503,
            code="RETRYABLE_UNAVAILABLE",
            message=exc.message,
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        sanitized_errors: list[dict[str, Any]] = []
        for err in exc.errors():
            # Sanitize: never echo rejected passwords or input secrets
            sanitized_errors.append(
                {
                    "loc": [str(x) for x in err.get("loc", ())],
                    "msg": str(err.get("msg", "")),
                    "type": str(err.get("type", "")),
                }
            )
        return make_error_response(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Request validation failed",
            details={"errors": sanitized_errors},
        )

    @app.exception_handler(DBAPIError)
    async def dbapi_exception_handler(_request: Request, exc: DBAPIError) -> JSONResponse:
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None)
        if sqlstate in ("55P03", "57014"):
            return make_error_response(
                status_code=503,
                code="RETRYABLE_UNAVAILABLE",
                message="Service temporarily unavailable, please retry",
                headers={"Retry-After": "1"},
            )
        return make_error_response(
            status_code=500,
            code="INTERNAL_ERROR",
            message="Internal server error",
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        return make_error_response(
            status_code=exc.status_code,
            code="HTTP_ERROR",
            message=str(exc.detail),
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, _exc: Exception) -> JSONResponse:
        return make_error_response(
            status_code=500,
            code="INTERNAL_ERROR",
            message="Internal server error",
        )

    # Healthcheck endpoint (E35, public, un-prefixed)
    @app.get("/healthz", response_model=None)
    async def healthz() -> dict[str, str]:
        current_settings = get_settings()
        return {"status": "ok", "version": current_settings.RELEASE_SHA}

    # Auth routes under /api/v1
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1")
    app.include_router(resources_router, prefix="/api/v1")
    app.include_router(bookings_router, prefix="/api/v1")

    return app


app: FastAPI = create_app()
