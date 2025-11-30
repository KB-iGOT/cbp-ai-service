from sqlalchemy import ARRAY, Boolean, Column, String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from ..core.database import Base
   
class User(Base):
    """Users model for user management"""
    __tablename__ = "users"
    
    user_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False
    )
    username = Column(
        String(100),
        nullable=False,
        unique=True,
        index=True
    )
    email = Column(
        String(255),
        nullable=False,
        unique=True,
        index=True
    )
    phone = Column(
        String(20),
        nullable=True
    )
    password_hash = Column(
        String(255),
        nullable=False
    )
    role_id = Column(
        UUID(as_uuid=True),
        ForeignKey("roles.role_id", ondelete="RESTRICT"),
        nullable=False,
        index=True
    )
    state_center_id = Column(
        String(32),
        nullable=True,
        index=True
    )
    department_id = Column(
        String(32),
        nullable=True,
        index=True
    )
    organization_ids = Column(
        ARRAY(String),
        nullable=True,
        default=list
    )
    is_active = Column(
        Boolean,
        default=True,
        nullable=False
    )
    last_login = Column(
        DateTime(timezone=True),
        nullable=True
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
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True
    )
    
    # Relationships
    role = relationship("Role", lazy="selectin",back_populates="users")
    creator = relationship("User", remote_side=[user_id])

    def __repr__(self):
        return f"<User(user_id={self.user_id}, username='{self.username}', email='{self.email}')>"
