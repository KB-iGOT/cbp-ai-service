import enum
import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..core.database import Base


class DesignationApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class DesignationApproval(Base):
    __tablename__ = "designation_approvals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rolemapping_id = Column(
        UUID(as_uuid=True),
        ForeignKey("role_mappings.id"),
        nullable=False,
        index=True,
    )
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    designation_name = Column(String(255), nullable=False, index=True)
    wing_division_section = Column(String(255), nullable=False)
    status = Column(
        String(20),
        default=DesignationApprovalStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    reviewer_comments = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    role_mapping = relationship("RoleMapping", back_populates="designation_approvals")
