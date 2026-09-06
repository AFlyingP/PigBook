import uuid

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import TSTZRANGE, UUID

from app.db.base import Base


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    resource_id = Column(UUID(as_uuid=True), ForeignKey("resources.id"), nullable=False)
    time_range = Column(TSTZRANGE, nullable=False)
    status = Column(Text, nullable=False, server_default=text("'waiting'"))
    offered_booking_id = Column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), unique=True, nullable=True
    )
    version = Column(Integer, nullable=False, server_default=text("1"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('waiting','offered','accepted','cancelled','expired')",
            name="waitlist_entries_status_check",
        ),
        CheckConstraint("version>0", name="waitlist_entries_version_check"),
        CheckConstraint(
            "NOT isempty(time_range) AND NOT lower_inf(time_range) AND NOT upper_inf(time_range) "
            "AND lower_inc(time_range) AND NOT upper_inc(time_range) "
            "AND isfinite(lower(time_range)) AND isfinite(upper(time_range))",
            name="waitlist_entries_time_range_check",
        ),
        CheckConstraint(
            "status NOT IN ('offered','accepted') OR offered_booking_id IS NOT NULL",
            name="waitlist_entries_offered_booking_check",
        ),
        Index(
            "waitlist_active_unique_idx",
            "user_id",
            "resource_id",
            "time_range",
            unique=True,
            postgresql_where=text("status IN ('waiting','offered')"),
        ),
        Index(
            "waitlist_fifo_idx",
            "resource_id",
            "created_at",
            "id",
            postgresql_where=text("status='waiting'"),
        ),
        Index("waitlist_owner_idx", "user_id", text("created_at DESC"), "id"),
    )
