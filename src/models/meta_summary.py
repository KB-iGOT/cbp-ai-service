from sqlalchemy import Column, ForeignKey, String, DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
import uuid

from ..core.database import Base

class MetaSummary(Base):
    """Meta summary of multiple document summaries."""
    __tablename__ = "meta_summaries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False)
    state_center_id = Column(
        String(32), 
        nullable=True, 
        index=True,
        comment="All documents must belong to this state/center"
    )
    department_id = Column(
        String(32),
        nullable=True, 
        index=True,
        comment="Optional: if set, all documents should belong to this department"
    )
    request_id = Column(UUID(as_uuid=True), unique=True, default=uuid.uuid4, nullable=False, index=True)
    file_ids = Column(JSONB, nullable=False, default=list)
    status = Column(String(32), nullable=False, default="PENDING", index=True)
    summary_text = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self):
        return f"<MetaSummary(request_id={self.request_id}, status='{self.status}')>"

