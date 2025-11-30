from sqlalchemy import Column, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from ..core.database import Base

# Model for suggested courses (add to models.py)
class SuggestedCourse(Base):
    """Suggested Courses model for storing user course suggestions based on role mappings"""
    __tablename__ = "suggested_courses"
    
    id = Column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4,
        nullable=False
    )

    user_id = Column(
        UUID(as_uuid=True), 
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    role_mapping_id = Column(
        UUID(as_uuid=True), 
        ForeignKey("role_mappings.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    
    # Store course identifiers as JSONB array
    course_identifiers = Column(
        JSONB,
        nullable=False,
        default=list,
        comment="JSONB: List of suggested course identifiers"
    )

    created_at = Column(
        DateTime(timezone=True), 
        server_default=func.now(),
        nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True), 
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )
    
    # Relationship
    role_mapping = relationship(
        "RoleMapping", 
        back_populates="suggested_courses"
    )
    
    def __repr__(self):
        return f"<SuggestedCourse(id={self.id}, role_mapping_id={self.role_mapping_id})>"
