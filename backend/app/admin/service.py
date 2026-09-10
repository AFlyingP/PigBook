import base64
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.audit import append_audit_log
from app.admin.schemas import (
    BlackoutCreate,
    EmptyBody,
    InviteCreate,
    ResourceCreate,
    ResourcePatch,
    UserPatch,
)
from app.auth.dependencies import AuthorizedScope, PreconditionRequiredError
from app.auth.models import Invitation, User
from app.auth.passwords import normalize_email
from app.auth.schemas import InvitationResult
from app.auth.schemas import User as UserSchema
from app.auth.service import EmailExistsError, revoke_user_refresh_tokens
from app.bookings.models import Booking
from app.bookings.schemas import Booking as BookingSchema
from app.bookings.schemas import BookingStatusFilter
from app.bookings.service import (
    InvalidState,
    ResourceInactive,
    SlotConflict,
    TooLate,
    VersionMismatch,
)
from app.bookings.service import (
    InvalidWindowError as BookingInvalidWindowError,
)
from app.config import get_settings
from app.resources.models import Resource as ResourceModel
from app.resources.schemas import Page, Resource
from app.resources.service import NotFoundError
from app.waitlist.models import WaitlistEntry


class ResourceInUse(Exception):
    def __init__(self, message: str = "Resource is in use") -> None:
        self.message = message
        super().__init__(message)


async def _check_resource_not_in_use(
    session: AsyncSession,
    resource_id: uuid.UUID,
    db_now: datetime,
) -> None:
    # 1. Any confirmed or offered booking with upper(range) > db_now
    stmt_b = select(func.count(Booking.id)).where(
        Booking.resource_id == resource_id,
        Booking.status.in_(["confirmed", "offered"]),
        func.upper(Booking.time_range) > db_now,
    )
    bkg_count = (await session.execute(stmt_b)).scalar_one()

    # 2. Any waiting waitlist entry
    stmt_w = select(func.count(WaitlistEntry.id)).where(
        WaitlistEntry.resource_id == resource_id,
        WaitlistEntry.status == "waiting",
    )
    wait_count = (await session.execute(stmt_w)).scalar_one()

    if bkg_count > 0 or wait_count > 0:
        raise ResourceInUse("Resource is currently in use by active bookings or waitlist entries")


async def list_admin_resources(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    limit: int = 25,
    offset: int = 0,
    active: bool | None = None,
) -> Page[Resource]:
    conditions = []
    if active is not None:
        conditions.append(ResourceModel.active.is_(active))

    count_stmt = select(func.count()).select_from(ResourceModel)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = select(ResourceModel)
    if conditions:
        stmt = stmt.where(*conditions)
    stmt = (
        stmt.order_by(ResourceModel.name.asc(), ResourceModel.id.asc()).limit(limit).offset(offset)
    )
    result = await session.execute(stmt)
    resources = result.scalars().all()
    items = [Resource.model_validate(r) for r in resources]

    return Page[Resource](
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def create_resource(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    data: ResourceCreate,
    now: datetime,
) -> Resource:
    """Create a new resource (E18)."""
    res_id = uuid.uuid4()
    resource = ResourceModel(
        id=res_id,
        name=data.name,
        description=data.description,
        location=data.location,
        active=True,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(resource)
    await session.flush()

    req_id = scope.predicates.get("request_id") or uuid.uuid4()
    await append_audit_log(
        session,
        action="admin.resource_create",
        target_type="resource",
        target_id=res_id,
        actor_id=scope.principal_id,
        request_id=req_id,
        details={"fields": ["description", "location", "name"]},
        now=now,
    )
    await session.flush()

    return Resource.model_validate(resource)


async def update_resource(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    data: ResourcePatch,
    now: datetime,
) -> Resource:
    """Guarded patch of a resource with optimistic locking (E19)."""
    if scope.object_id is None:
        raise NotFoundError("Resource not found")
    if scope.expected_version is None:
        raise PreconditionRequiredError("If-Match header is required")

    resource_id = scope.object_id

    # Exclusive lock on the resource row
    stmt = select(ResourceModel).where(ResourceModel.id == resource_id).with_for_update()
    res = await session.execute(stmt)
    resource = res.scalar_one_or_none()
    if resource is None:
        raise NotFoundError(f"Resource {resource_id} not found")

    # Stale version check strictly precedes state checks
    if resource.version != scope.expected_version:
        raise VersionMismatch("Resource has been modified by another request")

    old_active = bool(resource.active)

    patch_dict = data.model_dump(exclude_unset=True)
    if not patch_dict:
        raise ValueError("At least one field must be provided for update")

    # Sample DB clock for time decisions (Spec 5.1 / 5.2 / 5.3)
    db_clock = await session.scalar(select(func.clock_timestamp()))
    db_now = db_clock if db_clock is not None else now

    if patch_dict.get("active") is False and resource.active is True:
        await _check_resource_not_in_use(session, resource_id, db_now)

    update_stmt = (
        update(ResourceModel)
        .where(
            ResourceModel.id == resource_id,
            ResourceModel.version == scope.expected_version,
        )
        .values(
            **patch_dict,
            version=ResourceModel.version + 1,
            updated_at=db_now,
        )
        .returning(ResourceModel)
    )
    update_res = await session.execute(update_stmt)
    updated_resource = update_res.scalar_one_or_none()
    if updated_resource is None:
        raise VersionMismatch("Resource has been modified by another request")

    req_id = scope.predicates.get("request_id") or uuid.uuid4()
    audit_details: dict[str, Any] = {"fields": sorted(patch_dict.keys())}
    if "active" in patch_dict:
        audit_details["old_active"] = old_active
        audit_details["new_active"] = patch_dict["active"]
    await append_audit_log(
        session,
        action="admin.resource_patch",
        target_type="resource",
        target_id=resource_id,
        actor_id=scope.principal_id,
        request_id=req_id,
        details=audit_details,
        now=db_now,
    )
    await session.flush()

    return Resource.model_validate(updated_resource)


async def archive_resource(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    data: EmptyBody,
    now: datetime,
) -> Resource:
    """Soft archive resource by setting active=false (E20)."""
    if scope.object_id is None:
        raise NotFoundError("Resource not found")
    if scope.expected_version is None:
        raise PreconditionRequiredError("If-Match header is required")

    resource_id = scope.object_id

    # Exclusive lock on the resource row
    stmt = select(ResourceModel).where(ResourceModel.id == resource_id).with_for_update()
    res = await session.execute(stmt)
    resource = res.scalar_one_or_none()
    if resource is None:
        raise NotFoundError(f"Resource {resource_id} not found")

    # Stale version check strictly precedes state checks
    if resource.version != scope.expected_version:
        raise VersionMismatch("Resource has been modified by another request")

    old_active = bool(resource.active)

    db_clock = await session.scalar(select(func.clock_timestamp()))
    db_now = db_clock if db_clock is not None else now

    await _check_resource_not_in_use(session, resource_id, db_now)

    update_stmt = (
        update(ResourceModel)
        .where(
            ResourceModel.id == resource_id,
            ResourceModel.version == scope.expected_version,
        )
        .values(
            active=False,
            version=ResourceModel.version + 1,
            updated_at=db_now,
        )
        .returning(ResourceModel)
    )
    update_res = await session.execute(update_stmt)
    archived_resource = update_res.scalar_one_or_none()
    if archived_resource is None:
        raise VersionMismatch("Resource has been modified by another request")

    req_id = scope.predicates.get("request_id") or uuid.uuid4()
    await append_audit_log(
        session,
        action="admin.resource_archive",
        target_type="resource",
        target_id=resource_id,
        actor_id=scope.principal_id,
        request_id=req_id,
        details={"fields": ["active"], "new_active": False, "old_active": old_active},
        now=db_now,
    )
    await session.flush()

    return Resource.model_validate(archived_resource)


def validate_blackout_window(
    starts_at: datetime,
    ends_at: datetime,
    now: datetime,
) -> tuple[datetime, datetime]:
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        raise BookingInvalidWindowError("Timestamps must include an explicit timezone offset")

    starts_at_utc = starts_at.astimezone(timezone.utc)
    ends_at_utc = ends_at.astimezone(timezone.utc)

    if (
        starts_at_utc.second != 0
        or starts_at_utc.microsecond != 0
        or starts_at_utc.minute not in (0, 30)
    ):
        raise BookingInvalidWindowError(
            "starts_at must fall on a UTC 30-minute boundary with zero seconds and microseconds"
        )

    if ends_at_utc.second != 0 or ends_at_utc.microsecond != 0 or ends_at_utc.minute not in (0, 30):
        raise BookingInvalidWindowError(
            "ends_at must fall on a UTC 30-minute boundary with zero seconds and microseconds"
        )

    if ends_at_utc <= starts_at_utc:
        raise BookingInvalidWindowError("ends_at must be strictly greater than starts_at")

    duration = ends_at_utc - starts_at_utc
    if duration < timedelta(minutes=30) or duration > timedelta(days=30):
        raise BookingInvalidWindowError(
            "Blackout duration must be between 30 minutes and 30 days inclusive"
        )

    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)

    if ends_at_utc <= now_utc:
        raise BookingInvalidWindowError("ends_at must be in the future")

    current_half_hour = now_utc.replace(
        minute=(0 if now_utc.minute < 30 else 30), second=0, microsecond=0
    )
    if starts_at_utc < current_half_hour:
        raise BookingInvalidWindowError(
            "Blackout start time cannot be before current 30-minute window"
        )

    return starts_at_utc, ends_at_utc


async def create_blackout(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    data: BlackoutCreate,
    now: datetime,
) -> BookingSchema:
    """Create a blackout interval for a resource (E22)."""
    if scope.resource_id is None:
        raise NotFoundError("Resource not found")
    resource_id = scope.resource_id

    # 1. Acquire resource FOR SHARE and re-read it (Spec 5.1, 5.2)
    stmt = select(ResourceModel).where(ResourceModel.id == resource_id).with_for_update(read=True)
    res = await session.execute(stmt)
    resource = res.scalar_one_or_none()
    if resource is None:
        raise NotFoundError(f"Resource {resource_id} not found")
    if not resource.active:
        raise ResourceInactive(f"Resource {resource_id} is inactive")

    db_clock = await session.scalar(select(func.clock_timestamp()))
    db_now = db_clock if db_clock is not None else now

    starts_at_utc, ends_at_utc = validate_blackout_window(data.starts_at, data.ends_at, db_now)

    booking_id = uuid.uuid4()
    booking = Booking(
        id=booking_id,
        resource_id=resource_id,
        user_id=None,
        created_by=scope.principal_id,
        kind="blackout",
        time_range=Range(starts_at_utc, ends_at_utc, bounds="[)"),
        status="confirmed",
        expires_at=None,
        version=1,
        created_at=db_now,
        updated_at=db_now,
    )

    try:
        async with session.begin_nested():
            session.add(booking)
            await session.flush()
    except IntegrityError as exc:
        orig = getattr(exc, "orig", exc)
        cause = getattr(orig, "__cause__", None) or orig
        sqlstate = (
            getattr(cause, "sqlstate", None)
            or getattr(orig, "sqlstate", None)
            or getattr(orig, "pgcode", None)
        )
        constraint_name = getattr(cause, "constraint_name", None) or getattr(
            orig, "constraint_name", None
        )
        if sqlstate == "23P01" and constraint_name == "bookings_no_overlap":
            raise SlotConflict("Slot conflict: requested time interval is not available") from exc
        raise

    req_id = scope.predicates.get("request_id") or uuid.uuid4()
    await append_audit_log(
        session,
        action="admin.blackout_create",
        target_type="booking",
        target_id=booking_id,
        actor_id=scope.principal_id,
        request_id=req_id,
        details={
            "resource_id": str(resource_id),
            "starts_at": starts_at_utc.isoformat(),
            "ends_at": ends_at_utc.isoformat(),
        },
        now=db_now,
    )
    await session.flush()

    return BookingSchema.model_validate(booking)


async def cancel_blackout(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    data: EmptyBody,
    now: datetime,
) -> BookingSchema:
    """Soft cancel a blackout interval, promoting waiters atomically (E23)."""
    if scope.object_id is None:
        raise NotFoundError("Blackout not found")
    if scope.expected_version is None:
        raise PreconditionRequiredError("If-Match header is required")

    blackout_id = scope.object_id

    # 1. Lock resource FOR UPDATE
    if scope.resource_id is not None:
        lock_stmt = (
            select(ResourceModel.id).where(ResourceModel.id == scope.resource_id).with_for_update()
        )
        await session.execute(lock_stmt)

    # 2. Lock blackout row FOR UPDATE
    bkg_stmt = (
        select(Booking)
        .where(
            Booking.id == blackout_id,
            Booking.kind == "blackout",
        )
        .with_for_update()
    )
    blackout = (await session.execute(bkg_stmt)).scalar_one_or_none()
    if blackout is None:
        raise NotFoundError("Blackout not found")

    if scope.resource_id is None:
        lock_stmt = (
            select(ResourceModel.id)
            .where(ResourceModel.id == blackout.resource_id)
            .with_for_update()
        )
        await session.execute(lock_stmt)

    db_clock = await session.scalar(select(func.clock_timestamp()))
    db_now = db_clock if db_clock is not None else now

    if blackout.version != scope.expected_version:
        raise VersionMismatch("Blackout has been modified by another request")

    if blackout.status == "cancelled":
        return BookingSchema.model_validate(blackout)

    if blackout.status != "confirmed":
        raise InvalidState(f"Blackout in state '{blackout.status}' cannot be cancelled")

    if db_now >= blackout.time_range.upper:
        raise TooLate("A completed blackout cannot be cancelled")

    update_stmt = (
        update(Booking)
        .where(
            Booking.id == blackout_id,
            Booking.version == scope.expected_version,
        )
        .values(
            status="cancelled",
            version=Booking.version + 1,
            updated_at=db_now,
        )
        .returning(Booking)
    )
    res = await session.execute(update_stmt)
    updated_blackout = res.scalar_one_or_none()
    if updated_blackout is None:
        raise VersionMismatch("Blackout has been modified by another request")

    req_id = scope.predicates.get("request_id") or uuid.uuid4()
    await append_audit_log(
        session,
        action="admin.blackout_cancel",
        target_type="booking",
        target_id=blackout_id,
        actor_id=scope.principal_id,
        request_id=req_id,
        details={"resource_id": str(blackout.resource_id)},
        now=db_now,
    )

    from app.waitlist.service import promote_waiters

    res_id = scope.resource_id or uuid.UUID(str(blackout.resource_id))
    await promote_waiters(session, res_id, db_now)

    return BookingSchema.model_validate(updated_blackout)


async def list_resource_blackouts(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    resource_id: uuid.UUID,
    limit: int = 25,
    offset: int = 0,
) -> Page[BookingSchema]:
    stmt_r = select(ResourceModel.id).where(ResourceModel.id == resource_id)
    if (await session.execute(stmt_r)).scalar_one_or_none() is None:
        raise NotFoundError("Resource not found")

    conditions = [
        Booking.resource_id == resource_id,
        Booking.kind == "blackout",
    ]

    count_stmt = select(func.count()).select_from(Booking).where(*conditions)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Booking)
        .where(*conditions)
        .order_by(Booking.created_at.desc(), Booking.id.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    items = [BookingSchema.model_validate(b) for b in rows]

    return Page[BookingSchema](
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def list_admin_bookings(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    limit: int = 25,
    offset: int = 0,
    resource_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    status: BookingStatusFilter | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> Page[BookingSchema]:
    conditions = [
        Booking.kind == "reservation",
    ]
    if resource_id is not None:
        conditions.append(Booking.resource_id == resource_id)
    if user_id is not None:
        conditions.append(Booking.user_id == user_id)
    if status is not None:
        conditions.append(Booking.status == status.value)

    if (starts_at is None and ends_at is not None) or (starts_at is not None and ends_at is None):
        raise BookingInvalidWindowError("Date filters require both starts_at and ends_at")

    if starts_at is not None and ends_at is not None:
        if starts_at.tzinfo is None or ends_at.tzinfo is None:
            raise BookingInvalidWindowError("Timestamps must include explicit timezone offset")
        s_utc = starts_at.astimezone(timezone.utc)
        e_utc = ends_at.astimezone(timezone.utc)
        if e_utc <= s_utc:
            raise BookingInvalidWindowError("ends_at must be strictly greater than starts_at")
        if (e_utc - s_utc) > timedelta(days=90):
            raise BookingInvalidWindowError("Date filter window cannot exceed 90 days")
        conditions.append(Booking.time_range.op("&&")(func.tstzrange(s_utc, e_utc, "[)")))

    count_stmt = select(func.count()).select_from(Booking).where(*conditions)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(Booking)
        .where(*conditions)
        .order_by(Booking.created_at.desc(), Booking.id.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    items = [BookingSchema.model_validate(b) for b in rows]

    return Page[BookingSchema](
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_admin_booking(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    booking_id: uuid.UUID,
) -> BookingSchema:
    stmt = select(Booking).where(
        Booking.id == booking_id,
        Booking.kind == "reservation",
    )
    booking = (await session.execute(stmt)).scalar_one_or_none()
    if booking is None:
        raise NotFoundError("Booking not found")
    return BookingSchema.model_validate(booking)


async def create_invitation(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    data: InviteCreate,
    now: datetime,
    request_id: uuid.UUID | None = None,
) -> InvitationResult:
    """Create a single-use invitation for registration (E29)."""
    norm_email = normalize_email(data.email)

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

    req_id = request_id or scope.predicates.get("request_id") or uuid.uuid4()
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


async def list_admin_users(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    limit: int = 25,
    offset: int = 0,
    enabled: bool | None = None,
) -> Page[UserSchema]:
    """List all users in the system with optional enabled filter (E27)."""
    conditions = []
    if enabled is not None:
        conditions.append(User.enabled == enabled)

    count_stmt = select(func.count(User.id)).where(*conditions)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = (
        select(User)
        .where(*conditions)
        .order_by(User.created_at.desc(), User.id.asc())
        .limit(limit)
        .offset(offset)
    )
    users = (await session.execute(stmt)).scalars().all()
    items = [UserSchema.model_validate(u) for u in users]
    return Page[UserSchema](items=items, total=total, limit=limit, offset=offset)


async def update_user(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    data: UserPatch,
    now: datetime,
) -> UserSchema:
    """Guarded patch of a user with optimistic concurrency (E28)."""
    if scope.object_id is None:
        raise NotFoundError("User not found")
    if scope.expected_version is None:
        raise PreconditionRequiredError("If-Match header is required")

    user_id = scope.object_id
    expected_version = scope.expected_version

    stmt = select(User).where(User.id == user_id).with_for_update()
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()
    if user is None:
        raise NotFoundError("User not found")

    if user.version != expected_version:
        raise VersionMismatch("User version mismatch")

    patch_dict = data.model_dump(exclude_unset=True)
    if not patch_dict:
        raise ValueError("At least one field must be provided for update")

    old_role = str(user.role)
    old_enabled = bool(user.enabled)

    new_role = patch_dict.get("role", user.role)
    new_enabled = patch_dict.get("enabled", user.enabled)

    update_stmt = (
        update(User)
        .where(User.id == user_id, User.version == expected_version)
        .values(
            role=new_role,
            enabled=new_enabled,
            version=user.version + 1,
            updated_at=now,
        )
    )
    update_res = await session.execute(update_stmt)
    if getattr(update_res, "rowcount", None) != 1:
        raise VersionMismatch("User version mismatch")

    if old_enabled is True and new_enabled is False:
        await revoke_user_refresh_tokens(session, user_id=user_id, now=now)

    changed_fields = sorted(list(patch_dict.keys()))
    details: dict[str, Any] = {"fields": changed_fields}
    if "role" in patch_dict:
        details["old_role"] = old_role
        details["new_role"] = new_role
    if "enabled" in patch_dict:
        details["old_enabled"] = old_enabled
        details["new_enabled"] = new_enabled

    req_id = scope.predicates.get("request_id") or uuid.uuid4()
    await append_audit_log(
        session,
        action="admin.user_update",
        target_type="user",
        target_id=user_id,
        actor_id=scope.principal_id,
        request_id=req_id,
        details=details,
        now=now,
    )
    await session.flush()

    setattr(user, "role", new_role)
    setattr(user, "enabled", new_enabled)
    setattr(user, "version", expected_version + 1)
    setattr(user, "updated_at", now)

    return UserSchema.model_validate(user)
