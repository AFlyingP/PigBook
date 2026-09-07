import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthorizedScope
from app.bookings.models import Booking
from app.resources.models import Resource


class NotFoundError(Exception):
    def __init__(self, message: str = "Resource not found") -> None:
        self.message = message
        super().__init__(message)


class ResourceInactive(Exception):
    def __init__(self, message: str = "Resource is inactive") -> None:
        self.message = message
        super().__init__(message)


class SlotConflict(Exception):
    def __init__(
        self, message: str = "Slot conflict: requested time interval is not available"
    ) -> None:
        self.message = message
        super().__init__(message)


async def insert_confirmed(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    resource_id: uuid.UUID,
    time_range: Range,
    kind: str = "reservation",
    now: datetime | None = None,
) -> Booking:
    # 1. Acquire the resource row FOR SHARE and re-read it.
    stmt = select(Resource).where(Resource.id == resource_id).with_for_update(read=True)
    result = await session.execute(stmt)
    resource = result.scalar_one_or_none()

    # 2. Raise a typed NotFoundError if the resource does not exist.
    if resource is None:
        raise NotFoundError(f"Resource {resource_id} not found")

    # 3. Raise a typed ResourceInactive if the resource exists but active is false.
    if not resource.active:
        raise ResourceInactive(f"Resource {resource_id} is inactive")

    # 4. Insert the booking with status confirmed inside a savepoint, and flush().
    booking = Booking(
        id=uuid.uuid4(),
        resource_id=resource_id,
        user_id=scope.principal_id if kind == "reservation" else None,
        created_by=scope.principal_id,
        kind=kind,
        time_range=time_range,
        status="confirmed",
        expires_at=None,
        version=1,
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

    return booking
