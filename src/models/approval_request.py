import enum
import uuid
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..core.database import Base


class RequestStatus(str, enum.Enum):
    """Enum for approval request status"""
    DRAFT = "draft"
    PENDING = "pending"
    IN_REVIEW = "in_review"
    APPROVED_PUBLISHED = "approved_published"
    REJECTED = "rejected"


class RequestedRoleMapping(Base):
    """Model for storing approval request metadata"""
    __tablename__ = "requested_rolemappings"
    
    request_id = Column(
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
    request_name = Column(
        String(100),
        nullable=False
    )
    mdo_admin_id = Column(
        String(255),
        nullable=False
    )
    mdo_admin_name = Column(
        String(255),
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
    state_center_name = Column(
        String(255),
        nullable=False
    )
    department_name = Column(
        String(255),
        nullable=False
    )
    designation_count = Column(
        Integer,
        nullable=False
    )
    request_status = Column(
        String(30),
        default=RequestStatus.PENDING.value,
        nullable=False,
        index=True
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
    
    # Relationship to role mapping snapshots
    role_mapping_items = relationship(
        "RequestedRoleMappingItem",
        back_populates="approval_request",
        cascade="all, delete-orphan"
    )
    
    def __repr__(self):
        return f"<RequestedRoleMapping(request_id={self.request_id}, request_name='{self.request_name}', status='{self.request_status}')>"


class RequestedRoleMappingItem(Base):
    """Model for storing role mapping snapshots (immutable after creation)"""
    __tablename__ = "requested_rolemapping_items"
    
    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False
    )
    request_id = Column(
        UUID(as_uuid=True),
        ForeignKey("requested_rolemappings.request_id"),
        nullable=False,
        index=True
    )
    
    # Snapshot of role mapping data
    # NOTE: original_role_mapping_id has NO foreign key constraint to ensure data isolation
    original_role_mapping_id = Column(
        UUID(as_uuid=True),
        nullable=False,
        comment="Reference to original role mapping ID for tracking only (no FK constraint)"
    )
    designation_name = Column(
        String(255),
        nullable=False
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
    igot_designation_name = Column(
        String(255),
        nullable=False
    )
    igot_designation_id = Column(
        String(255),
        nullable=False
    )
    sort_order = Column(
        Integer,
        nullable=True
    )
    cbp_plans = Column(
        JSONB,
        default=list,
        nullable=True
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    
    # Relationship back to approval request
    approval_request = relationship(
        "RequestedRoleMapping",
        back_populates="role_mapping_items"
    )
    
    def __repr__(self):
        return f"<RequestedRoleMappingItem(id={self.id}, request_id={self.request_id}, designation='{self.designation_name}')>"
