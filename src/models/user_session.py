from sqlalchemy import Column, String, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
import uuid

from ..core.database import Base


class UserSession(Base):
    """Model for storing active user sessions with tokens.

    - Created on login (stores access_token and refresh_token)
    - Updated on refresh (new access_token)
    - Deleted on logout
    - Token validation checks if session exists
    """
    __tablename__ = "user_sessions"

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
    access_token_jti = Column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
        comment="JWT ID of the access token"
    )
    refresh_token_jti = Column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
        comment="JWT ID of the refresh token"
    )
    access_token = Column(
        Text,
        nullable=False,
        comment="Full access token string"
    )
    refresh_token = Column(
        Text,
        nullable=False,
        comment="Full refresh token string"
    )
    ip_address = Column(
        String(45),
        nullable=True,
        comment="Client IP address at login"
    )
    user_agent = Column(
        String(500),
        nullable=True,
        comment="Client user agent at login"
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Session creation time (login time)"
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="Last update time (token refresh time)"
    )
    access_token_expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="Access token expiration time"
    )
    refresh_token_expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="Refresh token expiration time"
    )

    def __repr__(self):
        return f"<UserSession(id={self.id}, user_id={self.user_id}, created_at={self.created_at})>"
