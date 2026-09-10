import re
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

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
    "Audit",
    "OutboxView",
    "OutboxStatusFilter",
    "FeedbackCreate",
    "Feedback",
    "FeedbackCreateResult",
    "SAFE_AUDIT_KEYS",
    "sanitize_audit_details",
    "extract_safe_error_category",
]

SAFE_AUDIT_KEYS = {
    "fields",
    "old_role",
    "new_role",
    "old_enabled",
    "new_enabled",
    "old_active",
    "new_active",
    "revoked_count",
    "previous_status",
    "attempts",
    "event_category",
    "reason_length",
    "user_id",
    "resource_id",
    "booking_id",
    "target_id",
    "accounts_anonymized",
    "feedback_deleted",
    "account_cutoff_days",
    "feedback_cutoff_days",
    "target_scope",
}


def sanitize_audit_details(details: dict[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {}
    sanitized: dict[str, Any] = {}
    for k, v in details.items():
        if k in SAFE_AUDIT_KEYS:
            if isinstance(v, (str, int, float, bool)) or v is None:
                sanitized[k] = v
            elif isinstance(v, list) and all(isinstance(item, (str, int)) for item in v):
                sanitized[k] = v
    return sanitized


def extract_safe_error_category(last_error: str | None) -> str | None:
    if not last_error:
        return None
    cleaned = last_error.strip()
    if ":" in cleaned:
        category = cleaned.split(":", 1)[0].strip()
    else:
        category = cleaned
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "", category)
    return safe[:50] or "unknown_error"


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


class Audit(BaseSchema):
    id: uuid.UUID
    actor_id: uuid.UUID | None = None
    action: str
    target_type: str
    target_id: uuid.UUID | None = None
    request_id: uuid.UUID
    details: dict[str, Any]
    created_at: datetime

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    @field_validator("details", mode="before")
    @classmethod
    def validate_details(cls, v: Any) -> dict[str, Any]:
        if isinstance(v, dict):
            return sanitize_audit_details(v)
        return {}


class OutboxStatusFilter(str, Enum):
    pending = "pending"
    processing = "processing"
    delivered = "delivered"
    dead = "dead"


class OutboxView(BaseSchema):
    id: uuid.UUID
    event_type: str
    aggregate_id: uuid.UUID
    status: str
    attempts: int
    occurred_at: datetime
    available_at: datetime
    last_error: str | None = None

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    @field_validator("last_error", mode="before")
    @classmethod
    def validate_last_error(cls, v: Any) -> str | None:
        if isinstance(v, str):
            return extract_safe_error_category(v)
        return None


class FeedbackCreate(BaseSchema):
    rating: int = Field(..., ge=1, le=5)
    task_completed: bool
    difficulty: str = Field(default="", max_length=2000)
    improvement: str = Field(default="", max_length=2000)
    consent_version: str | None = None
    consent: bool | None = None

    model_config = ConfigDict(extra="forbid")


class Feedback(BaseSchema):
    id: uuid.UUID
    user_id: uuid.UUID
    rating: int
    task_completed: bool
    difficulty: str
    improvement: str
    consent_version: str
    created_at: datetime

    model_config = ConfigDict(extra="forbid", from_attributes=True)


class FeedbackCreateResult(BaseSchema):
    id: uuid.UUID
    created_at: datetime

    model_config = ConfigDict(extra="forbid")
