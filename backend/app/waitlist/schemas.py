import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import ConfigDict, field_serializer, model_validator

from app.auth.schemas import BaseSchema


class WaitlistStatusFilter(str, Enum):
    waiting = "waiting"
    offered = "offered"
    accepted = "accepted"
    cancelled = "cancelled"
    expired = "expired"


class WaitCreate(BaseSchema):
    resource_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime

    model_config = ConfigDict(extra="forbid")


class AcceptOffer(BaseSchema):
    model_config = ConfigDict(extra="forbid")


class WaitEntry(BaseSchema):
    id: uuid.UUID
    user_id: uuid.UUID
    resource_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    status: str
    offered_booking_id: uuid.UUID | None = None
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
                "user_id": getattr(data, "user_id"),
                "resource_id": getattr(data, "resource_id"),
                "starts_at": data.time_range.lower,
                "ends_at": data.time_range.upper,
                "status": getattr(data, "status"),
                "offered_booking_id": getattr(data, "offered_booking_id"),
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
