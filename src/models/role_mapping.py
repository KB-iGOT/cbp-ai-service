import enum
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from ..core.database import Base

class ProcessingStatus(str, enum.Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
 
class RoleMapping(Base):
    """Role Mapping model for storing generated role mappings"""
    __tablename__ = "role_mappings"
    
    id = Column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4,
        nullable=False
    )
    user_id = Column(
        UUID(as_uuid=True), 
        nullable=False,
        index=True
    )
    org_type = Column(
        String(20),
        nullable=True,
        index=True,
        comment="Organization type: ministry or state"
    )
    state_center_id = Column(
        String(32),
        nullable=False,
        index=True
    )
    department_id = Column(
        String(32), 
        nullable=True,
        index=True
    )
    state_center_name = Column(
        String(255),
        nullable=True,
        index=True
    )
    department_name = Column(
        String(255), 
        nullable=True,
        index=True
    )

    status = Column(
        String(50), 
        default=ProcessingStatus.COMPLETED, 
        nullable=True,
        index=True,
        comment="Tracks the generation status: IN_PROGRESS, COMPLETED, FAILED"
    )
    
    error_message = Column(Text, nullable=True)

    sector_name = Column(
        String(255), 
        nullable=True,
        index=True
    )
    instruction = Column(
        Text, 
        nullable=True
    )
    designation_name = Column(
        String(255), 
        nullable=True,
        index=True
    )
    wing_division_section = Column(
        String(255), 
        nullable=True
    )
    role_responsibilities = Column(
        JSONB,
        default=list,
        nullable=True
    )
    activities = Column(
        JSONB,
        default=list,
        nullable=True
    )
    competencies = Column(
        JSONB,
        default=list,
        nullable=True
    )
    # NEW COLUMN: Sort order for hierarchical sorting
    sort_order = Column(
        Integer,
        nullable=True,
        index=True,
        comment="Sort order for hierarchical arrangement of designations (1=highest, higher numbers=lower hierarchy)"
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

    recommended_courses = relationship(
        "RecommendedCourse", 
        back_populates="role_mapping",
        cascade="all, delete-orphan"
    )

    cbp_plans = relationship(
        "CBPPlan", 
        back_populates="role_mapping",
        cascade="all, delete-orphan"
    )

    suggested_courses = relationship(
        "SuggestedCourse", 
        back_populates="role_mapping",
        cascade="all, delete-orphan"
    )

    user_added_courses = relationship(
        "UserAddedCourse", 
        back_populates="role_mapping",
        cascade="all, delete-orphan"
    )
    
    def __repr__(self):
        return f"<RoleMapping(id={self.id}, user_id='{self.user_id}', state_center_id='{self.state_center_id}')>"
