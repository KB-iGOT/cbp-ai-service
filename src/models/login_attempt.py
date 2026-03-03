from sqlalchemy import Column, String, DateTime, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
import uuid

from ..core.database import Base


class LoginAttempt(Base):
    """Model for tracking failed login attempts for brute-force protection.

    Uses a single row per username with an attempt counter instead of
    multiple rows per attempt.
    """
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
        unique=True,
        index=True
    )
    attempt_count = Column(
        Integer,
        default=1,
        nullable=False
    )
    last_attempt_ip = Column(
        String(45),  # Supports IPv6
        nullable=True
    )
    first_attempt_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    last_attempt_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        index=True
    )

    def __repr__(self):
        return f"<LoginAttempt(username='{self.username}', attempts={self.attempt_count}, last_attempt={self.last_attempt_at})>"
