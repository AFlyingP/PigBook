from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, field_validator

from app.auth.schemas import BaseSchema, InvitationResult
from app.resources.schemas import ResourceCreate, ResourcePatch

__all__ = [
    "InviteCreate",
    "InvitationResult",
    "ResourceCreate",
    "ResourcePatch",
    "EmptyBody",
    "BlackoutCreate",
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
