from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
import uuid

from ..core.database import Base

class Document(Base):
    """Single uploaded document (PDF) and its summary state."""
    __tablename__ = "documents"

    file_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False)
    state_center_id = Column(String(32), nullable=False, index=True)
    department_id = Column(String(32), nullable=True, index=True)
    uploader_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    filename = Column(String(512), nullable=False)
    document_name = Column(String(512), nullable=True, comment="Custom display name for the document, overrides filename if provided")
    stored_path = Column(String(1024), nullable=False)
    file_size_bytes = Column(Integer, nullable=True)
    summary_status = Column(String(32), nullable=False, default="NOT_STARTED", index=True)
    summary_text = Column(Text, nullable=True)
    summary_error = Column(Text, nullable=True)
    last_summary_request_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self):
        return f"<Document(file_id={self.file_id}, filename='{self.filename}', status='{self.summary_status}')>"
