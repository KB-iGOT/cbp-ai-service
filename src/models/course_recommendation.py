import enum
from sqlalchemy import  Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from ..core.database import Base

class RecommendationStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class RecommendedCourse(Base):
    """Recommended Courses model for storing course recommendations based on role mappings"""
    __tablename__ = "recommended_courses"
    
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

    status = Column(
        String, 
        default=RecommendationStatus.IN_PROGRESS,
        nullable=False
    )
    error_message = Column(Text, nullable=True)
    
    # Store the query and embedding used for recommendations
    vector_query = Column(
        Text,
        nullable=True,
        comment="Text query used for vector search"
    )

    embedding = Column(JSONB, nullable=True)
    
    # Store course recommendation results
    actual_courses = Column(
        JSONB,
        nullable=True,
        default=list,
        comment="All courses returned from vector search"
    )
    filtered_courses = Column(
        JSONB,
        nullable=True,
        default=list,
        comment="Filtered and ranked course recommendations"
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
        back_populates="recommended_courses"
    )

    cbp_plans = relationship(
        "CBPPlan", 
        back_populates="recommended_course"
    )
    
    def __repr__(self):
        return f"<RecommendedCourse(id={self.id}, role_mapping_id={self.role_mapping_id})>"
