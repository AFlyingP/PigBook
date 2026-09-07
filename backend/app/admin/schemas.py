from typing import Literal

from pydantic import field_validator

from app.auth.schemas import BaseSchema, InvitationResult

__all__ = ["InviteCreate", "InvitationResult"]


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
