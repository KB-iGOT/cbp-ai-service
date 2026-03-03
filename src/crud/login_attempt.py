from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import delete

from ..models.login_attempt import LoginAttempt
from ..core.configs import settings


class CRUDLoginAttempt:
    """
    CRUD methods for the LoginAttempt model.
    Handles tracking failed login attempts with a single row per username.
    """

    async def record_failed_attempt(
        self,
        db: AsyncSession,
        username: str,
        ip_address: Optional[str] = None
    ) -> LoginAttempt:
        """
        Record a failed login attempt. Creates a new row or increments existing counter.

        Args:
            db: Database session
            username: The username that failed login
            ip_address: Client IP address

        Returns:
            LoginAttempt: The created or updated record
        """
        username = username.lower().strip()

        # Check if record exists for this username
        result = await db.execute(
            select(LoginAttempt).where(LoginAttempt.username == username)
        )
        attempt = result.scalar_one_or_none()

        if attempt:
            # Check if lockout period has expired - if so, reset counter
            lockout_expired = self._is_lockout_expired(attempt.last_attempt_at)
            if lockout_expired:
                attempt.attempt_count = 1
                attempt.first_attempt_at = datetime.now(timezone.utc)
            else:
                attempt.attempt_count += 1

            attempt.last_attempt_ip = ip_address
            attempt.last_attempt_at = datetime.now(timezone.utc)
        else:
            # Create new record
            attempt = LoginAttempt(
                username=username,
                attempt_count=1,
                last_attempt_ip=ip_address
            )
            db.add(attempt)

        await db.commit()
        await db.refresh(attempt)
        return attempt

    def _is_lockout_expired(self, last_attempt_at: datetime) -> bool:
        """Check if the lockout period has expired since last attempt."""
        if not last_attempt_at:
            return True

        # Ensure timezone aware comparison
        if last_attempt_at.tzinfo is None:
            last_attempt_at = last_attempt_at.replace(tzinfo=timezone.utc)

        lockout_end = last_attempt_at + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
        return datetime.now(timezone.utc) >= lockout_end

    async def get_attempt_record(
        self,
        db: AsyncSession,
        username: str
    ) -> Optional[LoginAttempt]:
        """Get the login attempt record for a username."""
        username = username.lower().strip()
        result = await db.execute(
            select(LoginAttempt).where(LoginAttempt.username == username)
        )
        return result.scalar_one_or_none()

    async def get_failed_attempts_count(
        self,
        db: AsyncSession,
        username: str
    ) -> int:
        """
        Get the current failed attempt count for a username.
        Returns 0 if no record exists or lockout has expired.
        """
        attempt = await self.get_attempt_record(db, username)

        if not attempt:
            return 0

        # If lockout period expired, consider attempts as 0
        if self._is_lockout_expired(attempt.last_attempt_at):
            return 0

        return attempt.attempt_count

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
        attempt = await self.get_attempt_record(db, username)

        if not attempt:
            return False, None

        # Check if lockout expired
        if self._is_lockout_expired(attempt.last_attempt_at):
            return False, None

        # Check if max attempts reached
        if attempt.attempt_count >= settings.MAX_LOGIN_ATTEMPTS:
            # Ensure timezone aware
            last_attempt = attempt.last_attempt_at
            if last_attempt.tzinfo is None:
                last_attempt = last_attempt.replace(tzinfo=timezone.utc)

            lockout_end = last_attempt + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
            now = datetime.now(timezone.utc)

            remaining_seconds = (lockout_end - now).total_seconds()
            remaining_minutes = max(1, int(remaining_seconds / 60) + 1)
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
            Number of remaining attempts
        """
        failed_count = await self.get_failed_attempts_count(db, username)
        remaining = settings.MAX_LOGIN_ATTEMPTS - failed_count
        return max(0, remaining)

    async def clear_failed_attempts(
        self,
        db: AsyncSession,
        username: str
    ) -> bool:
        """
        Clear failed login attempts for a username (delete the record).
        Called after successful login or admin unlock.

        Args:
            db: Database session
            username: The username to clear attempts for

        Returns:
            bool: True if record was deleted, False if not found
        """
        username = username.lower().strip()
        result = await db.execute(
            delete(LoginAttempt).where(LoginAttempt.username == username)
        )
        await db.commit()
        return result.rowcount > 0


# Initialize the CRUD utility for use across the application
crud_login_attempt = CRUDLoginAttempt()
