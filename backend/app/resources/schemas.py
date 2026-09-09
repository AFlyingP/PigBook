import uuid
from datetime import datetime, timezone
from typing import Any, Generic, Literal, TypeVar

from pydantic import ConfigDict, Field, field_serializer, model_validator

from app.auth.schemas import BaseSchema

T = TypeVar("T")


class Resource(BaseSchema):
    id: uuid.UUID
    name: str
    description: str
    location: str
    active: bool
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    @field_serializer("created_at", "updated_at")
    def serialize_dt(self, dt: datetime, _info: Any) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Page(BaseSchema, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class OccupiedInterval(BaseSchema):
    starts_at: datetime
    ends_at: datetime
    kind: Literal["reservation", "blackout"]
    status: Literal["confirmed", "offered"]

    @field_serializer("starts_at", "ends_at")
    def serialize_dt(self, dt: datetime, _info: Any) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Availability(BaseSchema):
    resource_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    timezone: Literal["America/New_York"] = "America/New_York"
    occupied: list[OccupiedInterval]

    @field_serializer("starts_at", "ends_at")
    def serialize_dt(self, dt: datetime, _info: Any) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ResourceCreate(BaseSchema):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    location: str = Field(..., min_length=1, max_length=200)

    model_config = ConfigDict(extra="forbid")


class ResourcePatch(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    location: str | None = Field(default=None, min_length=1, max_length=200)
    active: bool | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def validate_patch_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for field in ("name", "description", "location", "active"):
            if field in data and data[field] is None:
                raise ValueError(f"Field '{field}' cannot be null")
        provided = [f for f in ("name", "description", "location", "active") if f in data]
        if not provided:
            raise ValueError("At least one field must be provided for update")
        return data
