import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.models import AuditLog


async def append_audit_log(
    session: AsyncSession,
    *,
    action: str,
    target_type: str,
    request_id: uuid.UUID | str,
    actor_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> AuditLog:
    """Append an immutable audit entry to the audit log.

    This function only flushes the new record to the current session;
    it never commits the transaction.
    """
    if isinstance(request_id, str):
        req_uuid = uuid.UUID(request_id)
    else:
        req_uuid = request_id

    entry = AuditLog(
        id=uuid.uuid4(),
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        request_id=req_uuid,
        details=details if details is not None else {},
        created_at=now if now is not None else datetime.now(timezone.utc),
    )
    session.add(entry)
    await session.flush()
    return entry
