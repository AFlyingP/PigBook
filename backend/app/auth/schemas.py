import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_serializer


class BaseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class User(BaseSchema):
    id: uuid.UUID
    email: str
    display_name: str
    role: str
    enabled: bool
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


class Login(BaseSchema):
    email: str
    password: str


class TokenResponse(BaseSchema):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = 900
    user: User
