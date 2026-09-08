import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import ConfigDict, Field, field_serializer, model_validator

from app.auth.schemas import BaseSchema


class BookingStatusFilter(str, Enum):
    """Booking statuses a caller can filter their own reservations by.

    `pending` is a transient in-transaction state that is never committed by the booking
    services, so it is not an accepted filter value.
    """

    confirmed = "confirmed"
    offered = "offered"
    cancelled = "cancelled"
    expired = "expired"


class StoredResponse(BaseSchema):
    status: int
    body: dict[str, Any]
    headers: dict[str, str]

    model_config = ConfigDict(extra="forbid")


class Booking(BaseSchema):
    id: uuid.UUID
    resource_id: uuid.UUID
    user_id: uuid.UUID | None = None
    kind: str
    starts_at: datetime
    ends_at: datetime
    status: str
    expires_at: datetime | None = None
    cancellation_reason: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    @model_validator(mode="before")
    @classmethod
    def extract_from_orm(cls, data: Any) -> Any:
        if hasattr(data, "time_range") and hasattr(data.time_range, "lower"):
            return {
                "id": getattr(data, "id"),
                "resource_id": getattr(data, "resource_id"),
                "user_id": getattr(data, "user_id"),
                "kind": getattr(data, "kind"),
                "starts_at": data.time_range.lower,
                "ends_at": data.time_range.upper,
                "status": getattr(data, "status"),
                "expires_at": getattr(data, "expires_at"),
                "cancellation_reason": getattr(data, "cancellation_reason"),
                "version": getattr(data, "version"),
                "created_at": getattr(data, "created_at"),
                "updated_at": getattr(data, "updated_at"),
            }
        return data

    @field_serializer("starts_at", "ends_at", "created_at", "updated_at")
    def serialize_dt(self, dt: datetime, _info: Any) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @field_serializer("expires_at")
    def serialize_opt_dt(self, dt: datetime | None, _info: Any) -> str | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class BookingCreate(BaseSchema):
    resource_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime

    model_config = ConfigDict(extra="forbid")


class Cancel(BaseSchema):
    reason: str = Field(default="", max_length=500)

    model_config = ConfigDict(extra="forbid")
