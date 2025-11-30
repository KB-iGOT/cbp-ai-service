from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from ..core.database import Base

class UserAddedCourse(Base):
    """User Added Courses model for storing user-added courses from external sources"""
    __tablename__ = "user_added_courses"
    
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

    identifier = Column(
        UUID(as_uuid=True), 
        nullable=False,
        index=True,
        unique=True
    )
    
    # Course details
    name = Column(
        String(500),
        nullable=False,
        comment="Full name of the course"
    )
    platform = Column(
        String(100),
        nullable=False,
        comment="Platform where the course is hosted (e.g., Coursera, edX, Udemy)"
    )
    public_link = Column(
        Text,
        nullable=False,
        comment="Public URL to the specific course"
    )
    
    # Optional fields
    relevancy = Column(
        Integer,
        nullable=True,
        comment="Relevancy score from 0 to 100"
    )
    rationale = Column(
        Text,
        nullable=True,
        comment="Brief explanation of why this course is essential"
    )
    language = Column(
        String(10),
        nullable=True,
        comment="Language of the course (e.g., en, hi)"
    )
    
    # Competencies as JSONB
    competencies = Column(
        JSONB,
        nullable=True,
        default=list,
        comment="Array of competency objects with competencyAreaName, competencyThemeName, competencySubThemeName"
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
    
    role_mapping = relationship(
        "RoleMapping", 
        back_populates="user_added_courses"
    )
    
    def __repr__(self):
        return f"<UserAddedCourse(id={self.id}, name='{self.name}', platform='{self.platform}')>"
