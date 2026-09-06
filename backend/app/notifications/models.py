import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base


class Outbox(Base):
    __tablename__ = "outbox"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type = Column(Text, nullable=False)
    aggregate_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False)
    aggregate_version = Column(Integer, nullable=False)
    payload = Column(JSONB, nullable=False)
    status = Column(Text, nullable=False, server_default=text("'pending'"))
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    occurred_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    available_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    lease_until = Column(DateTime(timezone=True), nullable=True)
    lease_token = Column(UUID(as_uuid=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "aggregate_id",
            "aggregate_version",
            "event_type",
            name="outbox_aggregate_id_aggregate_version_event_type_key",
        ),
        CheckConstraint(
            "event_type IN ("
            "'booking_confirmed','booking_cancelled','waitlist_offered','hold_expired'"
            ")",
            name="outbox_event_type_check",
        ),
        CheckConstraint("jsonb_typeof(payload)='object'", name="outbox_payload_check"),
        CheckConstraint(
            "status IN ('pending','processing','delivered','dead')", name="outbox_status_check"
        ),
        CheckConstraint("attempts>=0", name="outbox_attempts_check"),
        CheckConstraint(
            "(status='processing' AND lease_until IS NOT NULL AND lease_token IS NOT NULL) OR "
            "(status<>'processing' AND lease_until IS NULL AND lease_token IS NULL)",
            name="outbox_lease_check",
        ),
        Index(
            "outbox_pending_idx",
            "available_at",
            "occurred_at",
            "id",
            postgresql_where=text("status='pending'"),
        ),
        Index(
            "outbox_lease_idx", "lease_until", "id", postgresql_where=text("status='processing'")
        ),
    )


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("outbox.id"), nullable=False)
    recipient_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    channel = Column(Text, nullable=False)
    state = Column(Text, nullable=False)
    provider_message_id = Column(Text, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "recipient_id",
            "channel",
            name="notification_deliveries_event_id_recipient_id_channel_key",
        ),
        CheckConstraint("channel='email'", name="notification_deliveries_channel_check"),
        CheckConstraint(
            "state IN ('pending','sent','skipped')", name="notification_deliveries_state_check"
        ),
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeat"

    name = Column(Text, primary_key=True)
    seen_at = Column(DateTime(timezone=True), nullable=False)
    expiry_scan_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (CheckConstraint("name='primary'", name="worker_heartbeat_name_check"),)
