from sqlalchemy import Column, String, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from ..core.database import Base

class StateCenterData(Base):
    """State Center Data model for storing PDF summaries"""
    __tablename__ = "state_center_data"
    
    id = Column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4,
        nullable=False
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
    acbp_plan_filename = Column(
        String(255), 
        nullable=True
    )
    work_allocation_filename = Column(
        String(255), 
        nullable=True
    )
    acbp_plan_summary = Column(
        Text, 
        nullable=True
    )
    work_allocation_order_summary = Column(
        Text, 
        nullable=True
    )
    status = Column(String, default="pending")  
    error_message = Column(Text, nullable=True)  # ✅ Store failure reason
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
    
    def __repr__(self):
        return f"<StateCenterData id={self.id} state_center_id={self.state_center_id} status={self.status}>"
