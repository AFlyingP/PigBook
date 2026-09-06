import uuid

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(Text, nullable=False, unique=True)
    password_hash = Column(Text, nullable=False)
    display_name = Column(Text, nullable=False)
    role = Column(Text, nullable=False, server_default=text("'member'"))
    enabled = Column(Boolean, nullable=False, server_default=text("true"))
    version = Column(Integer, nullable=False, server_default=text("1"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("role IN ('member','admin')", name="users_role_check"),
        CheckConstraint("version>0", name="users_version_check"),
        CheckConstraint(
            "email=lower(btrim(email)) AND length(email)<=254", name="users_email_check"
        ),
        CheckConstraint("length(display_name) BETWEEN 1 AND 80", name="users_display_name_check"),
    )


class Invitation(Base):
    __tablename__ = "invitations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(Text, nullable=False)
    token_hash = Column(CHAR(64), nullable=False, unique=True)
    role = Column(Text, nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    consumed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("role IN ('member','admin')", name="invitations_role_check"),
        CheckConstraint(
            "email=lower(btrim(email)) AND length(email)<=254", name="invitations_email_check"
        ),
        CheckConstraint("expires_at>created_at", name="invitations_expires_at_check"),
        Index("invitations_email_idx", "email"),
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    token_hash = Column(CHAR(64), nullable=False, unique=True)
    family_id = Column(UUID(as_uuid=True), nullable=False)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("refresh_tokens.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    family_expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "expires_at>created_at AND expires_at<=family_expires_at",
            name="refresh_tokens_expiry_check",
        ),
        Index(
            "refresh_one_child_idx",
            "parent_id",
            unique=True,
            postgresql_where=text("parent_id IS NOT NULL"),
        ),
        Index("refresh_family_idx", "family_id"),
        Index("refresh_user_idx", "user_id"),
    )


class RateLimit(Base):
    __tablename__ = "rate_limits"

    scope = Column(Text, primary_key=True, nullable=False)
    identity_hash = Column(CHAR(64), primary_key=True, nullable=False)
    window_start = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    count = Column(Integer, nullable=False)

    __table_args__ = (CheckConstraint("count>0", name="rate_limits_count_check"),)
