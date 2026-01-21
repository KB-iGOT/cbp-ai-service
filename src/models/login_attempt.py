from sqlalchemy import Column, String, DateTime, Integer, Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
import uuid

from ..core.database import Base


class LoginAttempt(Base):
    """Model for tracking login attempts for brute-force protection"""
    __tablename__ = "login_attempts"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False
    )
    username = Column(
        String(255),
        nullable=False,
        index=True
    )
    ip_address = Column(
        String(45),  # Supports IPv6
        nullable=True
    )
    attempt_time = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True
    )
    is_successful = Column(
        Boolean,
        default=False,
        nullable=False
    )
    failure_reason = Column(
        String(100),
        nullable=True
    )

    def __repr__(self):
        return f"<LoginAttempt(username='{self.username}', successful={self.is_successful}, time={self.attempt_time})>"
