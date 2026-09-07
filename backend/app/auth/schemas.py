import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator


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


class Register(BaseSchema):
    invitation_token: str
    email: str
    password: str
    display_name: str

    @field_validator("email")
    @classmethod
    def validate_email_address(cls, v: str) -> str:
        from app.auth.passwords import normalize_email

        try:
            return normalize_email(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("password")
    @classmethod
    def validate_password_codepoints(cls, v: str) -> str:
        from app.auth.passwords import validate_password_length

        try:
            return validate_password_length(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, v: str) -> str:
        cleaned = v.strip()
        if len(cleaned) < 1 or len(cleaned) > 80:
            raise ValueError("Display name length must be between 1 and 80 characters")
        return cleaned


class InvitationResult(BaseSchema):
    id: uuid.UUID
    email: str
    role: str
    expires_at: datetime
    invitation_url: str

    @field_serializer("expires_at")
    def serialize_dt(self, dt: datetime, _info: Any) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
