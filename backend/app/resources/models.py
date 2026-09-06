import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class Resource(Base):
    __tablename__ = "resources"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=False, server_default=text("''"))
    location = Column(Text, nullable=False)
    active = Column(Boolean, nullable=False, server_default=text("true"))
    version = Column(Integer, nullable=False, server_default=text("1"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 100", name="resources_name_check"),
        CheckConstraint("length(description)<=2000", name="resources_description_check"),
        CheckConstraint("length(location) BETWEEN 1 AND 200", name="resources_location_check"),
        CheckConstraint("version>0", name="resources_version_check"),
        Index("resources_active_name_idx", "active", "name", "id"),
    )
