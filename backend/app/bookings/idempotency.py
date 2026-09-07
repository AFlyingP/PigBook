import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AuthorizedScope
from app.bookings.models import IdempotencyKey
from app.bookings.schemas import StoredResponse

__all__ = ["execute_create"]


class IdempotencyKeyRequired(Exception):
    def __init__(self, message: str = "Idempotency-Key header is required") -> None:
        self.message = message
        super().__init__(message)


class IdempotencyKeyInvalid(Exception):
    def __init__(self, message: str = "Idempotency-Key must be a valid UUID v4") -> None:
        self.message = message
        super().__init__(message)


class IdempotencyKeyMismatch(Exception):
    def __init__(self, message: str = "Idempotency key payload mismatch") -> None:
        self.message = message
        super().__init__(message)


class IncompleteIdempotencyRecord(Exception):
    def __init__(self, message: str = "Incomplete idempotency record") -> None:
        self.message = message
        super().__init__(message)


def _normalize_path(path: str) -> str:
    segments = path.split("/")
    normalized_segments: list[str] = []
    for seg in segments:
        try:
            u = uuid.UUID(seg)
            normalized_segments.append(str(u).lower())
        except (ValueError, TypeError, AttributeError):
            normalized_segments.append(seg)
    return "/".join(normalized_segments)


def _normalize_value(val: Any) -> Any:
    if isinstance(val, BaseModel):
        return _normalize_value(val.model_dump())
    elif isinstance(val, dict):
        return {k: _normalize_value(v) for k, v in val.items()}
    elif isinstance(val, (list, tuple)):
        return [_normalize_value(item) for item in val]
    elif isinstance(val, datetime):
        if val.tzinfo is None:
            dt = val.replace(tzinfo=timezone.utc)
        else:
            dt = val.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    elif isinstance(val, uuid.UUID):
        return str(val).lower()
    return val


def _compute_request_hash(method: str, path: str, body: BaseModel) -> str:
    canonical_dict = {
        "body": _normalize_value(body),
        "method": method.upper(),
        "path": _normalize_path(path),
    }
    canonical_json = json.dumps(
        canonical_dict,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _parse_idempotency_key(key_header: str | None) -> uuid.UUID:
    if key_header is None or not key_header.strip():
        raise IdempotencyKeyRequired("Idempotency-Key header is required")

    trimmed = key_header.strip()
    try:
        parsed = uuid.UUID(trimmed)
    except (ValueError, TypeError, AttributeError):
        raise IdempotencyKeyInvalid("Idempotency-Key must be a valid UUID v4")

    if parsed.version != 4:
        raise IdempotencyKeyInvalid("Idempotency-Key must be a valid UUID v4")

    return parsed


async def execute_create(
    session: AsyncSession,
    *,
    scope: AuthorizedScope,
    key: uuid.UUID,
    method: str,
    path: str,
    body: BaseModel,
    operation: Callable[[], Awaitable[StoredResponse]],
    now: datetime,
) -> StoredResponse:
    if scope.principal_id is None:
        raise RuntimeError("Principal ID is required for idempotency scoping")
    user_id = scope.principal_id

    req_hash = _compute_request_hash(method=method, path=path, body=body)

    for _ in range(5):
        # 1. Attempt key acquisition
        insert_stmt = text(
            """
            INSERT INTO idempotency_keys (user_id, key, request_hash, created_at, expires_at)
            SELECT :user_id, :key, :request_hash, sampled.at, sampled.at + interval '24 hours'
            FROM (SELECT clock_timestamp() AS at) sampled
            ON CONFLICT (user_id, key) DO NOTHING
            RETURNING key;
            """
        )
        res = await session.execute(
            insert_stmt,
            {"user_id": user_id, "key": key, "request_hash": req_hash},
        )
        acquired = res.scalar_one_or_none()

        if acquired is not None:
            # Key acquired: execute fresh operation
            stored = await operation()

            persisted_headers = {
                k: v for k, v in stored.headers.items() if k.lower() in ("location", "etag")
            }

            update_stmt = (
                update(IdempotencyKey)
                .where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
                .values(
                    response_status=stored.status,
                    response_body=stored.body,
                    response_headers=persisted_headers,
                )
            )
            await session.execute(update_stmt)

            ret_headers = dict(stored.headers)
            ret_headers["Idempotency-Replayed"] = "false"
            return StoredResponse(
                status=stored.status,
                body=stored.body,
                headers=ret_headers,
            )

        # 2. Key exists: lock row FOR UPDATE to block on concurrent holder
        select_stmt = (
            select(
                IdempotencyKey,
                (IdempotencyKey.expires_at <= func.clock_timestamp()).label("is_expired"),
            )
            .where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
            .with_for_update()
        )
        row_res = await session.execute(select_stmt)
        locked_row = row_res.one_or_none()

        if locked_row is None:
            # Concurrent holder rolled back before committing; retry acquisition
            continue

        existing_key: IdempotencyKey = locked_row[0]
        is_expired: bool = locked_row[1]

        if is_expired:
            delete_stmt = delete(IdempotencyKey).where(
                IdempotencyKey.user_id == user_id,
                IdempotencyKey.key == key,
            )
            await session.execute(delete_stmt)
            continue

        # Check hash match
        if existing_key.request_hash != req_hash:
            raise IdempotencyKeyMismatch("Idempotency key payload mismatch")

        # Invariant check: response_status IS NULL on a committed visible row
        if existing_key.response_status is None:
            raise IncompleteIdempotencyRecord("Incomplete idempotency record")

        stored_headers = (
            dict(existing_key.response_headers) if existing_key.response_headers is not None else {}
        )
        stored_headers["Idempotency-Replayed"] = "true"

        stored_body = existing_key.response_body if existing_key.response_body is not None else {}

        return StoredResponse(
            status=int(existing_key.response_status),
            body=dict(stored_body),
            headers=stored_headers,
        )

    raise IncompleteIdempotencyRecord("Unable to acquire or replay idempotency key")
