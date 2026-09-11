import re
import secrets
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import jwt
from fastapi import Depends, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.auth.rate_limit import check_mutation_rate_limit, check_read_rate_limit
from app.bookings.models import Booking
from app.config import get_settings
from app.db.session import get_session
from app.waitlist.models import WaitlistEntry


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


class ObjectNotFoundError(Exception):
    """Raised when an addressed object is absent or owned by another principal."""

    def __init__(self, message: str = "Not found") -> None:
        self.message = message
        super().__init__(message)


class PreconditionRequiredError(Exception):
    """Raised when a route requiring If-Match is called without one."""

    def __init__(self, message: str = "If-Match header is required") -> None:
        self.message = message
        super().__init__(message)


class InvalidIfMatchError(Exception):
    """Raised when an If-Match header is present but not a single strong entity tag."""

    def __init__(self, message: str = "If-Match header is malformed") -> None:
        self.message = message
        super().__init__(message)


class LastAdminError(Exception):
    def __init__(
        self, message: str = "Cannot demote or disable the last active administrator"
    ) -> None:
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
    assert_current: Callable[[AsyncSession], Awaitable[None]] | None = None
    predicates: dict[Any, Any] = field(default_factory=dict)


_STRONG_ETAG_RE = re.compile(r'^"(0|[1-9][0-9]*)"$')


def _parse_if_match(header_value: str | None) -> int:
    """Parse a required If-Match header into the version it pins.

    Only a single strong entity tag of the form `"<version>"` is accepted, matching the
    ETag emitted by single-object reads and mutations. The weak form `W/"<version>"` is
    rejected because If-Match uses strong comparison, and `*` is rejected because these
    routes need the caller's concrete expected version rather than mere existence.
    """
    if header_value is None or not header_value.strip():
        raise PreconditionRequiredError("If-Match header is required")

    match = _STRONG_ETAG_RE.match(header_value.strip())
    if match is None:
        raise InvalidIfMatchError(
            'If-Match must be a single strong entity tag of the form "<version>"'
        )
    return int(match.group(1))


def _enforce_policy_role(policy: Policy, role: str | None) -> None:
    """Enforce the role half of an authenticated policy, failing closed.

    Every policy that admits an authenticated principal must name its accepted roles
    here. A policy with no rule yet is rejected rather than admitted, so adding a member
    to `Policy` cannot silently skip the role check.
    """
    allowed: tuple[str, ...]
    if policy is Policy.admin:
        allowed = ("admin",)
    elif policy in (Policy.authenticated, Policy.own_booking, Policy.own_waitlist):
        allowed = ("member", "admin")
    else:
        # `public` is answered before authentication; the remaining policies have no
        # route registered yet and must not be admitted by default.
        raise ForbiddenError("Insufficient permissions")

    if role not in allowed:
        raise ForbiddenError("Insufficient permissions")


policy_registry: dict[str, Policy] = {
    "E01": Policy.public,
    "E02": Policy.public,
    "E03": Policy.public,
    "E04": Policy.public,
    "E05": Policy.authenticated,
    "E06": Policy.authenticated,
    "E07": Policy.authenticated,
    "E08": Policy.authenticated,
    "E09": Policy.authenticated,
    "E10": Policy.own_booking,
    "E11": Policy.own_booking,
    "E12": Policy.own_booking,
    "E13": Policy.authenticated,
    "E14": Policy.own_waitlist,
    "E15": Policy.own_waitlist,
    "E16": Policy.own_waitlist,
    "E17": Policy.admin,
    "E18": Policy.admin,
    "E19": Policy.admin,
    "E20": Policy.admin,
    "E21": Policy.admin,
    "E22": Policy.admin,
    "E23": Policy.admin,
    "E24": Policy.admin,
    "E25": Policy.admin,
    "E26": Policy.admin,
    "E27": Policy.admin,
    "E28": Policy.admin,
    "E29": Policy.admin,
    "E30": Policy.admin,
    "E31": Policy.admin,
    "E32": Policy.admin,
    "E33": Policy.authenticated,
    "E34": Policy.admin,
    "E35": Policy.public,
    "E36": Policy.public,
    "E37": Policy.metrics,
}


async def _resolve_own_booking_scope(
    request: Request,
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    assert_current: Callable[[AsyncSession], Awaitable[None]],
) -> AuthorizedScope:
    """Resolve the own-booking scope for the addressed reservation.

    `own` means the authenticated principal's own reservations for members and admins
    alike: a booking belonging to somebody else, a blackout, or an unknown id is not
    found. Only owner-scoped identifiers are read here; no object row is locked, so the
    service can take the resource lock before the booking row (Spec 5.1).
    """
    booking_predicate = (Booking.user_id == principal_id,)
    predicates: dict[Any, Any] = {
        "booking": booking_predicate,
        "bookings": booking_predicate,
        Booking: booking_predicate,
    }

    raw_booking_id = request.path_params.get("id")
    if raw_booking_id is None:
        # Collection route: the scope is the principal's own reservations.
        return AuthorizedScope(
            principal_id=principal_id,
            policy=Policy.own_booking,
            assert_current=assert_current,
            predicates=predicates,
        )

    # Mutating own-booking routes carry optimistic concurrency through If-Match, and the
    # header is validated before any object is addressed.
    expected_version: int | None = None
    if request.method != "GET":
        expected_version = _parse_if_match(request.headers.get("If-Match"))

    try:
        booking_id = uuid.UUID(str(raw_booking_id))
    except (ValueError, TypeError):
        raise RequestValidationError(
            [
                {
                    "type": "uuid_parsing",
                    "loc": ("path", "id"),
                    "msg": "Input should be a valid UUID",
                }
            ]
        ) from None

    try:
        stmt = select(Booking.id, Booking.resource_id).where(
            Booking.id == booking_id,
            Booking.kind == "reservation",
            Booking.user_id == principal_id,
        )
        row = (await session.execute(stmt)).one_or_none()
    finally:
        # Release the read transaction's physical connection before the handler opens its
        # own transaction.
        await session.rollback()

    if row is None:
        raise ObjectNotFoundError("Booking not found")

    return AuthorizedScope(
        principal_id=principal_id,
        policy=Policy.own_booking,
        object_id=row.id,
        resource_id=row.resource_id,
        expected_version=expected_version,
        assert_current=assert_current,
        predicates=predicates,
    )


async def _resolve_own_waitlist_scope(
    request: Request,
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    assert_current: Callable[[AsyncSession], Awaitable[None]],
) -> AuthorizedScope:
    """Resolve the own-waitlist scope for the addressed waitlist entry.

    `own` means the authenticated principal's own waitlist entries for members and admins
    alike. Only owner-scoped identifiers are read here; no object row is locked, so the
    service can take the resource lock before the waitlist row (Spec 5.1).
    """
    waitlist_predicate = (WaitlistEntry.user_id == principal_id,)
    predicates: dict[Any, Any] = {
        "waitlist": waitlist_predicate,
        "waitlist_entry": waitlist_predicate,
        "waitlist_entries": waitlist_predicate,
        WaitlistEntry: waitlist_predicate,
    }

    raw_entry_id = request.path_params.get("id")
    if raw_entry_id is None:
        # Collection route: the scope is the principal's own waitlist entries.
        return AuthorizedScope(
            principal_id=principal_id,
            policy=Policy.own_waitlist,
            assert_current=assert_current,
            predicates=predicates,
        )

    expected_version: int | None = None
    if request.method != "GET":
        expected_version = _parse_if_match(request.headers.get("If-Match"))

    try:
        entry_id = uuid.UUID(str(raw_entry_id))
    except (ValueError, TypeError):
        raise RequestValidationError(
            [
                {
                    "type": "uuid_parsing",
                    "loc": ("path", "id"),
                    "msg": "Input should be a valid UUID",
                }
            ]
        ) from None

    try:
        stmt = select(WaitlistEntry.id, WaitlistEntry.resource_id).where(
            WaitlistEntry.id == entry_id,
            WaitlistEntry.user_id == principal_id,
        )
        row = (await session.execute(stmt)).one_or_none()
    finally:
        await session.rollback()

    if row is None:
        raise ObjectNotFoundError("Waitlist entry not found")

    return AuthorizedScope(
        principal_id=principal_id,
        policy=Policy.own_waitlist,
        object_id=row.id,
        resource_id=row.resource_id,
        expected_version=expected_version,
        assert_current=assert_current,
        predicates=predicates,
    )


async def _resolve_admin_scope(
    request: Request,
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    assert_current: Callable[[AsyncSession], Awaitable[None]],
) -> AuthorizedScope:
    """Resolve the administrative scope for E17–E34 routes.

    Extracts path IDs, looks up linked resource IDs without row locks where required,
    parses If-Match for mutating endpoints, and carries authorization predicates.
    """
    request_time = datetime.now(timezone.utc)
    req_method = request.scope.get("method", "GET")
    if req_method == "GET":
        await check_read_rate_limit(principal_id, request_time)
    else:
        await check_mutation_rate_limit(principal_id, request_time)

    raw_id = (
        request.path_params.get("id")
        if hasattr(request, "path_params") and request.path_params
        else None
    )
    path = request.scope.get("path", "")
    object_id: uuid.UUID | None = None
    resource_id: uuid.UUID | None = None
    expected_version: int | None = None
    predicates: dict[Any, Any] = {}

    req_id: uuid.UUID | None = None
    if hasattr(request.state, "request_id") and request.state.request_id:
        try:
            req_id = uuid.UUID(str(request.state.request_id))
        except (ValueError, TypeError):
            pass

    requires_if_match = (req_method in ("PATCH", "DELETE") and raw_id is not None) or (
        req_method == "POST" and path.endswith("/cancel")
    )
    if requires_if_match:
        expected_version = _parse_if_match(request.headers.get("If-Match"))

    if raw_id is not None:
        try:
            parsed_id = uuid.UUID(str(raw_id))
        except (ValueError, TypeError):
            raise RequestValidationError(
                [
                    {
                        "type": "uuid_parsing",
                        "loc": ("path", "id"),
                        "msg": "Input should be a valid UUID",
                    }
                ]
            ) from None

        if "/admin/resources/" in path:
            object_id = parsed_id
            resource_id = parsed_id
        elif "/admin/blackouts/" in path:
            object_id = parsed_id
            try:
                stmt = select(Booking.id, Booking.resource_id).where(
                    Booking.id == parsed_id,
                    Booking.kind == "blackout",
                )
                row = (await session.execute(stmt)).one_or_none()
            finally:
                await session.rollback()
            if row is None:
                raise ObjectNotFoundError("Blackout not found")
            resource_id = row.resource_id
        elif "/admin/bookings/" in path:
            object_id = parsed_id
            try:
                stmt = select(Booking.id, Booking.resource_id).where(
                    Booking.id == parsed_id,
                    Booking.kind == "reservation",
                )
                row = (await session.execute(stmt)).one_or_none()
            finally:
                await session.rollback()
            if row is None:
                raise ObjectNotFoundError("Booking not found")
            resource_id = row.resource_id
            predicates = {
                "booking": (),
                "allow_running": True,
                "audit": True,
                "request_id": req_id,
            }
        elif "/admin/users/" in path:
            object_id = parsed_id
            if req_method == "PATCH":
                try:
                    user_lookup_stmt = select(User.id, User.version).where(User.id == parsed_id)
                    u_row = (await session.execute(user_lookup_stmt)).one_or_none()
                finally:
                    await session.rollback()

                if u_row is None:
                    raise ObjectNotFoundError("User not found")

                # Stale version check strictly precedes state checks
                if expected_version is not None and u_row[1] != expected_version:
                    from app.bookings.service import VersionMismatch

                    raise VersionMismatch("User version mismatch")

                actor_id = principal_id
                target_id = parsed_id

                async def _assert_current_user_e28(target_session: AsyncSession) -> None:
                    # 1. Transactional advisory lock 714001 FIRST (Spec 5.1)
                    await target_session.execute(text("SELECT pg_advisory_xact_lock(714001)"))

                    # 2. Lock actor and target in UUID order
                    if actor_id == target_id:
                        stmt = select(User).where(User.id == target_id).with_for_update()
                        t_user = (await target_session.execute(stmt)).scalar_one_or_none()
                        a_user = t_user
                    elif actor_id < target_id:
                        a_stmt = select(User).where(User.id == actor_id).with_for_update(read=True)
                        a_user = (await target_session.execute(a_stmt)).scalar_one_or_none()
                        t_stmt = select(User).where(User.id == target_id).with_for_update()
                        t_user = (await target_session.execute(t_stmt)).scalar_one_or_none()
                    else:
                        t_stmt = select(User).where(User.id == target_id).with_for_update()
                        t_user = (await target_session.execute(t_stmt)).scalar_one_or_none()
                        a_stmt = select(User).where(User.id == actor_id).with_for_update(read=True)
                        a_user = (await target_session.execute(a_stmt)).scalar_one_or_none()

                    if a_user is None or not a_user.enabled or a_user.role != "admin":
                        raise InvalidTokenError("Invalid or expired token")

                    if t_user is None:
                        raise ObjectNotFoundError("User not found")

                    if expected_version is not None and t_user.version != expected_version:
                        from app.bookings.service import VersionMismatch

                        raise VersionMismatch("User version mismatch")

                    body_json = await request.json()
                    patch_role = body_json.get("role") if isinstance(body_json, dict) else None
                    patch_enabled = (
                        body_json.get("enabled") if isinstance(body_json, dict) else None
                    )

                    is_act_admin = t_user.role == "admin" and t_user.enabled is True
                    rem_admin = True
                    if patch_role is not None and patch_role != "admin":
                        rem_admin = False
                    if patch_enabled is not None and patch_enabled is False:
                        rem_admin = False

                    if is_act_admin and not rem_admin:
                        c_stmt = select(func.count(User.id)).where(
                            User.role == "admin", User.enabled.is_(True)
                        )
                        cnt = (await target_session.execute(c_stmt)).scalar_one()
                        if cnt <= 1:
                            raise LastAdminError(
                                "Cannot demote or disable the last active administrator"
                            )

                assert_current = _assert_current_user_e28
            else:
                try:
                    user_get_stmt = select(User.id).where(User.id == parsed_id)
                    get_row = (await session.execute(user_get_stmt)).one_or_none()
                finally:
                    await session.rollback()
                if get_row is None:
                    raise ObjectNotFoundError("User not found")
        elif "/admin/outbox/" in path:
            object_id = parsed_id

    if path.rstrip("/").endswith("/admin/bookings"):
        predicates = {
            "booking": (),
            "allow_running": True,
        }

    if "request_id" not in predicates and req_id is not None:
        predicates["request_id"] = req_id

    return AuthorizedScope(
        principal_id=principal_id,
        policy=Policy.admin,
        object_id=object_id,
        resource_id=resource_id,
        expected_version=expected_version,
        assert_current=assert_current,
        predicates=predicates,
    )


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

        if policy == Policy.metrics:
            settings = get_settings()
            metrics_token = settings.METRICS_TOKEN
            if not metrics_token or not secrets.compare_digest(token, metrics_token):
                raise InvalidTokenError("Invalid metrics token")
            return AuthorizedScope(principal_id=None, policy=policy)

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
        try:
            stmt = select(User.id, User.role, User.enabled).where(User.id == user_id)
            result = await session.execute(stmt)
            user_row = result.one_or_none()
        finally:
            # Release read transaction's physical connection immediately so it is not
            # held while subsequent request lifecycle dependencies or handlers execute.
            await session.rollback()

        if user_row is None:
            raise InvalidTokenError("Invalid or expired token")

        db_id, db_role, db_enabled = user_row
        if not db_enabled:
            raise InvalidTokenError("Invalid or expired token")

        _enforce_policy_role(policy, db_role)

        async def _assert_current(target_session: AsyncSession) -> None:
            """In-transaction policy revalidation selecting user FOR SHARE (Spec 3.4, 5.1)."""
            reval_stmt = (
                select(User.id, User.role, User.enabled)
                .where(User.id == db_id)
                .with_for_update(read=True)
            )
            reval_res = await target_session.execute(reval_stmt)
            current_row = reval_res.one_or_none()
            if current_row is None or not current_row[2]:
                raise InvalidTokenError("Invalid or expired token")
            _enforce_policy_role(policy, current_row[1])

        if policy in (Policy.own_booking, Policy.own_waitlist):
            # The own-object budget is consumed and committed before ownership is
            # resolved, so a request rejected by the dependency still costs the caller
            # its bucket (Spec 8.2). The rate-limit transaction is separate and already
            # committed, so no bucket row is held while the domain transaction takes the
            # user, resource, booking or waitlist locks.
            request_time = datetime.now(timezone.utc)
            if request.method == "GET":
                await check_read_rate_limit(db_id, request_time)
            else:
                await check_mutation_rate_limit(db_id, request_time)

            if policy == Policy.own_booking:
                return await _resolve_own_booking_scope(
                    request,
                    session,
                    principal_id=db_id,
                    assert_current=_assert_current,
                )
            else:
                return await _resolve_own_waitlist_scope(
                    request,
                    session,
                    principal_id=db_id,
                    assert_current=_assert_current,
                )

        if policy == Policy.admin:
            return await _resolve_admin_scope(
                request,
                session,
                principal_id=db_id,
                assert_current=_assert_current,
            )

        return AuthorizedScope(
            principal_id=db_id,
            policy=policy,
            assert_current=_assert_current,
        )

    return dependency
