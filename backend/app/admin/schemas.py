from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, field_validator, model_validator

from app.auth.schemas import BaseSchema, InvitationResult
from app.resources.schemas import ResourceCreate, ResourcePatch

__all__ = [
    "InviteCreate",
    "InvitationResult",
    "ResourceCreate",
    "ResourcePatch",
    "EmptyBody",
    "BlackoutCreate",
    "UserPatch",
]


class EmptyBody(BaseSchema):
    model_config = ConfigDict(extra="forbid")


class BlackoutCreate(BaseSchema):
    starts_at: datetime
    ends_at: datetime

    model_config = ConfigDict(extra="forbid")


class InviteCreate(BaseSchema):
    email: str
    role: Literal["member", "admin"] = "member"

    @field_validator("email")
    @classmethod
    def validate_email_address(cls, v: str) -> str:
        from app.auth.passwords import normalize_email

        try:
            return normalize_email(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class UserPatch(BaseSchema):
    role: Literal["member", "admin"] | None = None
    enabled: bool | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_patch(self) -> "UserPatch":
        if "role" in self.model_fields_set and self.role is None:
            raise ValueError("role cannot be null")
        if "enabled" in self.model_fields_set and self.enabled is None:
            raise ValueError("enabled cannot be null")
        if self.role is None and self.enabled is None:
            raise ValueError("At least one of role or enabled must be provided")
        return self
