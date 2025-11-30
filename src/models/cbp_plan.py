from sqlalchemy import Column, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from ..core.database import Base

class CBPPlan(Base):
    """CBP Plans model for storing user-selected course plans based on role mappings"""
    __tablename__ = "cbp_plans"
    
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

    recommended_course_id = Column(
        UUID(as_uuid=True), 
        ForeignKey("recommended_courses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Source recommendation record"
    )

    # Selected courses data (JSONB)
    selected_courses = Column(
        JSONB,
        nullable=False,
        default=list,
        comment="JSONB: Selected course details from filtered recommendations"
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
    
    # Relationships
    role_mapping = relationship(
        "RoleMapping", 
        back_populates="cbp_plans"
    )
    recommended_course = relationship(
        "RecommendedCourse",
        back_populates="cbp_plans"
    )
    
    def __repr__(self):
        return (
            f"<CBPPlan(id={self.id}, "
            f"role_mapping_id={self.role_mapping_id}, "
            f"recommended_course_id={self.recommended_course_id})>"
        )
