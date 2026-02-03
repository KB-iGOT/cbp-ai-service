from datetime import datetime, timezone
from typing import Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import delete

from ..models.user_session import UserSession
from ..core.logger import logger


class CRUDUserSession:
    """
    CRUD methods for the UserSession model.
    Handles session management for login/logout/refresh token flows.
    """

    async def create_session(
        self,
        db: AsyncSession,
        user_id: UUID,
        access_token: str,
        refresh_token: str,
        access_token_jti: str,
        refresh_token_jti: str,
        access_token_expires_at: datetime,
        refresh_token_expires_at: datetime,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> UserSession:
        """
        Create a new user session on login.

        Args:
            db: Database session
            user_id: User ID
            access_token: Full access token string
            refresh_token: Full refresh token string
            access_token_jti: Access token JTI
            refresh_token_jti: Refresh token JTI
            access_token_expires_at: Access token expiration
            refresh_token_expires_at: Refresh token expiration
            ip_address: Client IP address
            user_agent: Client user agent

        Returns:
            UserSession: The created session
        """
        session = UserSession(
            user_id=user_id,
            access_token=access_token,
            refresh_token=refresh_token,
            access_token_jti=access_token_jti,
            refresh_token_jti=refresh_token_jti,
            access_token_expires_at=access_token_expires_at,
            refresh_token_expires_at=refresh_token_expires_at,
            ip_address=ip_address,
            user_agent=user_agent
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        logger.info(f"Session created for user_id: {user_id}")
        return session

    async def get_session_by_access_token_jti(
        self,
        db: AsyncSession,
        access_token_jti: str
    ) -> Optional[UserSession]:
        """Get session by access token JTI."""
        result = await db.execute(
            select(UserSession).where(UserSession.access_token_jti == access_token_jti)
        )
        return result.scalar_one_or_none()

    async def get_session_by_refresh_token_jti(
        self,
        db: AsyncSession,
        refresh_token_jti: str
    ) -> Optional[UserSession]:
        """Get session by refresh token JTI."""
        result = await db.execute(
            select(UserSession).where(UserSession.refresh_token_jti == refresh_token_jti)
        )
        return result.scalar_one_or_none()

    async def is_access_token_valid(
        self,
        db: AsyncSession,
        access_token_jti: str
    ) -> bool:
        """
        Check if access token exists in an active session.
        Returns False if session doesn't exist (logged out).
        """
        session = await self.get_session_by_access_token_jti(db, access_token_jti)
        return session is not None

    async def is_refresh_token_valid(
        self,
        db: AsyncSession,
        refresh_token_jti: str
    ) -> bool:
        """
        Check if refresh token exists in an active session.
        Returns False if session doesn't exist (logged out).
        """
        session = await self.get_session_by_refresh_token_jti(db, refresh_token_jti)
        return session is not None

    async def update_session_access_token(
        self,
        db: AsyncSession,
        refresh_token_jti: str,
        new_access_token: str,
        new_access_token_jti: str,
        new_access_token_expires_at: datetime
    ) -> Optional[UserSession]:
        """
        Update session with new access token on refresh.

        Args:
            db: Database session
            refresh_token_jti: JTI of the refresh token (to find the session)
            new_access_token: New access token string
            new_access_token_jti: New access token JTI
            new_access_token_expires_at: New access token expiration

        Returns:
            UserSession: Updated session or None if not found
        """
        session = await self.get_session_by_refresh_token_jti(db, refresh_token_jti)
        if not session:
            logger.warning(f"Session not found for refresh_token_jti: {refresh_token_jti}")
            return None

        session.access_token = new_access_token
        session.access_token_jti = new_access_token_jti
        session.access_token_expires_at = new_access_token_expires_at
        session.updated_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(session)
        logger.info(f"Session updated with new access token for user_id: {session.user_id}")
        return session

    async def delete_session_by_access_token_jti(
        self,
        db: AsyncSession,
        access_token_jti: str
    ) -> bool:
        """
        Delete session by access token JTI (logout).

        Args:
            db: Database session
            access_token_jti: Access token JTI

        Returns:
            bool: True if session was deleted, False if not found
        """
        result = await db.execute(
            delete(UserSession).where(UserSession.access_token_jti == access_token_jti)
        )
        await db.commit()
        deleted = result.rowcount > 0
        if deleted:
            logger.info(f"Session deleted for access_token_jti: {access_token_jti}")
        return deleted

    async def delete_session_by_refresh_token_jti(
        self,
        db: AsyncSession,
        refresh_token_jti: str
    ) -> bool:
        """
        Delete session by refresh token JTI.

        Args:
            db: Database session
            refresh_token_jti: Refresh token JTI

        Returns:
            bool: True if session was deleted, False if not found
        """
        result = await db.execute(
            delete(UserSession).where(UserSession.refresh_token_jti == refresh_token_jti)
        )
        await db.commit()
        deleted = result.rowcount > 0
        if deleted:
            logger.info(f"Session deleted for refresh_token_jti: {refresh_token_jti}")
        return deleted

    async def delete_all_user_sessions(
        self,
        db: AsyncSession,
        user_id: UUID
    ) -> int:
        """
        Delete all sessions for a user (logout from all devices).

        Args:
            db: Database session
            user_id: User ID

        Returns:
            int: Number of sessions deleted
        """
        result = await db.execute(
            delete(UserSession).where(UserSession.user_id == user_id)
        )
        await db.commit()
        logger.info(f"Deleted {result.rowcount} sessions for user_id: {user_id}")
        return result.rowcount

    async def cleanup_expired_sessions(
        self,
        db: AsyncSession
    ) -> int:
        """
        Remove sessions where refresh token has expired.
        Should be run periodically.

        Args:
            db: Database session

        Returns:
            int: Number of expired sessions removed
        """
        now = datetime.now(timezone.utc)
        result = await db.execute(
            delete(UserSession).where(UserSession.refresh_token_expires_at < now)
        )
        await db.commit()
        logger.info(f"Cleaned up {result.rowcount} expired sessions")
        return result.rowcount

    async def get_user_active_sessions_count(
        self,
        db: AsyncSession,
        user_id: UUID
    ) -> int:
        """Get count of active sessions for a user."""
        from sqlalchemy import func
        result = await db.execute(
            select(func.count(UserSession.id)).where(UserSession.user_id == user_id)
        )
        return result.scalar() or 0


# Initialize the CRUD utility for use across the application
crud_user_session = CRUDUserSession()
