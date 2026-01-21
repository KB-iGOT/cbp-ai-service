from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, delete

from ..models.login_attempt import LoginAttempt
from ..core.configs import settings


class CRUDLoginAttempt:
    """
    CRUD methods for the LoginAttempt model.
    Handles tracking and querying of login attempts for brute-force protection.
    """

    async def record_attempt(
        self,
        db: AsyncSession,
        username: str,
        is_successful: bool,
        ip_address: Optional[str] = None,
        failure_reason: Optional[str] = None
    ) -> LoginAttempt:
        """Record a login attempt (successful or failed)."""
        attempt = LoginAttempt(
            username=username.lower().strip(),
            ip_address=ip_address,
            is_successful=is_successful,
            failure_reason=failure_reason
        )
        db.add(attempt)
        await db.commit()
        await db.refresh(attempt)
        return attempt

    async def get_recent_failed_attempts_count(
        self,
        db: AsyncSession,
        username: str,
        within_minutes: Optional[int] = None
    ) -> int:
        """
        Get the count of recent failed login attempts for a username.

        Args:
            db: Database session
            username: The username to check
            within_minutes: Time window in minutes (defaults to LOGIN_LOCKOUT_MINUTES from settings)

        Returns:
            Count of failed attempts within the time window
        """
        if within_minutes is None:
            within_minutes = settings.LOGIN_LOCKOUT_MINUTES

        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)

        result = await db.execute(
            select(func.count(LoginAttempt.id))
            .where(
                LoginAttempt.username == username.lower().strip(),
                LoginAttempt.is_successful == False,
                LoginAttempt.attempt_time >= cutoff_time
            )
        )
        return result.scalar() or 0

    async def is_account_locked(
        self,
        db: AsyncSession,
        username: str
    ) -> tuple[bool, Optional[int]]:
        """
        Check if an account is locked due to too many failed attempts.

        Args:
            db: Database session
            username: The username to check

        Returns:
            Tuple of (is_locked: bool, remaining_minutes: Optional[int])
        """
        failed_count = await self.get_recent_failed_attempts_count(db, username)

        if failed_count >= settings.MAX_LOGIN_ATTEMPTS:
            # Get the most recent failed attempt to calculate remaining lockout time
            result = await db.execute(
                select(LoginAttempt.attempt_time)
                .where(
                    LoginAttempt.username == username.lower().strip(),
                    LoginAttempt.is_successful == False
                )
                .order_by(LoginAttempt.attempt_time.desc())
                .limit(1)
            )
            last_attempt_time = result.scalar()

            if last_attempt_time:
                lockout_end = last_attempt_time + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
                now = datetime.now(timezone.utc)

                if now < lockout_end:
                    remaining_seconds = (lockout_end - now).total_seconds()
                    remaining_minutes = int(remaining_seconds / 60) + 1
                    return True, remaining_minutes

        return False, None

    async def get_remaining_attempts(
        self,
        db: AsyncSession,
        username: str
    ) -> int:
        """
        Get the number of remaining login attempts before lockout.

        Args:
            db: Database session
            username: The username to check

        Returns:
            Number of remaining attempts (0 if locked)
        """
        failed_count = await self.get_recent_failed_attempts_count(db, username)
        remaining = settings.MAX_LOGIN_ATTEMPTS - failed_count
        return max(0, remaining)

    async def clear_failed_attempts(
        self,
        db: AsyncSession,
        username: str
    ) -> int:
        """
        Clear all failed login attempts for a username (used after successful login or admin unlock).

        Args:
            db: Database session
            username: The username to clear attempts for

        Returns:
            Number of records deleted
        """
        result = await db.execute(
            delete(LoginAttempt)
            .where(
                LoginAttempt.username == username.lower().strip(),
                LoginAttempt.is_successful == False
            )
        )
        await db.commit()
        return result.rowcount

    async def cleanup_old_attempts(
        self,
        db: AsyncSession,
        older_than_days: int = 30
    ) -> int:
        """
        Clean up old login attempt records.

        Args:
            db: Database session
            older_than_days: Delete records older than this many days

        Returns:
            Number of records deleted
        """
        cutoff_time = datetime.now(timezone.utc) - timedelta(days=older_than_days)

        result = await db.execute(
            delete(LoginAttempt)
            .where(LoginAttempt.attempt_time < cutoff_time)
        )
        await db.commit()
        return result.rowcount


# Initialize the CRUD utility for use across the application
crud_login_attempt = CRUDLoginAttempt()
