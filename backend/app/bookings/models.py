import uuid

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, UUID, ExcludeConstraint

from app.db.base import Base


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resource_id = Column(UUID(as_uuid=True), ForeignKey("resources.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    kind = Column(Text, nullable=False, server_default=text("'reservation'"))
    time_range = Column(TSTZRANGE, nullable=False)
    status = Column(Text, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    cancellation_reason = Column(Text, nullable=True)
    version = Column(Integer, nullable=False, server_default=text("1"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('reservation','blackout')", name="bookings_kind_check"),
        CheckConstraint(
            "status IN ('pending','confirmed','offered','cancelled','expired')",
            name="bookings_status_check",
        ),
        CheckConstraint(
            "cancellation_reason IS NULL OR length(cancellation_reason)<=500",
            name="bookings_cancellation_reason_check",
        ),
        CheckConstraint("version>0", name="bookings_version_check"),
        CheckConstraint(
            "NOT isempty(time_range) AND NOT lower_inf(time_range) AND NOT upper_inf(time_range) "
            "AND lower_inc(time_range) AND NOT upper_inc(time_range) "
            "AND isfinite(lower(time_range)) AND isfinite(upper(time_range))",
            name="bookings_time_range_check",
        ),
        CheckConstraint(
            "(kind='reservation' AND user_id IS NOT NULL) OR "
            "(kind='blackout' AND user_id IS NULL AND status IN ('confirmed','cancelled'))",
            name="bookings_kind_user_status_check",
        ),
        CheckConstraint(
            "(status='offered' AND expires_at IS NOT NULL AND expires_at<=lower(time_range)) OR "
            "(status<>'offered' AND expires_at IS NULL)",
            name="bookings_offered_expiry_check",
        ),
        ExcludeConstraint(
            ("resource_id", "="),
            ("time_range", "&&"),
            where=text("status IN ('confirmed','offered')"),
            name="bookings_no_overlap",
        ),
        Index("bookings_owner_idx", "user_id", text("created_at DESC"), "id"),
        Index("bookings_resource_start_idx", "resource_id", text("lower(time_range)"), "id"),
        Index("bookings_expiry_idx", "expires_at", "id", postgresql_where=text("status='offered'")),
    )


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True, nullable=False)
    key = Column(UUID(as_uuid=True), primary_key=True, nullable=False)
    request_hash = Column(CHAR(64), nullable=False)
    response_status = Column(SmallInteger, nullable=True)
    response_body = Column(JSONB, nullable=True)
    response_headers = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "expires_at=created_at+interval '24 hours'", name="idempotency_keys_expires_at_check"
        ),
        CheckConstraint(
            "(response_status IS NULL AND response_body IS NULL AND response_headers IS NULL) OR "
            "("
            "response_status BETWEEN 200 AND 499 AND "
            "response_body IS NOT NULL AND response_headers IS NOT NULL"
            ")",
            name="idempotency_keys_response_check",
        ),
        Index("idempotency_expiry_idx", "expires_at"),
    )
