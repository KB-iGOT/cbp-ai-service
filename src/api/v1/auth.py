from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...schemas.auth import LogoutResponse, RefreshTokenRequest, RefreshTokenResponse, TokenResponse
from ...models.user import User

from ...core.security import (
    authenticate_user,
    create_user_session,
    refresh_user_session,
    invalidate_session,
    update_last_login
)
from ...core.database import get_db_session
from ...core.configs import settings
from ...core.logger import logger
from ...crud.login_attempt import crud_login_attempt
from ...crud.user_session import crud_user_session

from ...api.dependencies import get_current_user, get_current_user_with_token, require_role


router = APIRouter(tags=["Authentication"])


# Helper function to get client IP
def get_client_ip(request: Request) -> str:
    """Extract client IP from request, handling proxies."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def get_user_agent(request: Request) -> str:
    """Extract user agent from request."""
    return request.headers.get("User-Agent", "unknown")


# Auth APIs
@router.post("/auth/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login(
    request: Request,
    username: str = Form(..., min_length=3, max_length=255),
    password: str = Form(..., min_length=8),
    db: AsyncSession = Depends(get_db_session)
):
    """
    User login endpoint

    Authenticates user with username/email and password.
    Creates a session and returns access token and refresh token.

    Includes brute-force protection:
    - Tracks failed login attempts
    - Locks account after MAX_LOGIN_ATTEMPTS failed attempts
    - Auto-unlocks after LOGIN_LOCKOUT_MINUTES

    Args:
        request: FastAPI request object
        username: Username or email
        password: User password
        db: Database session

    Returns:
        TokenResponse: Access token, refresh token, and user info
    """
    try:
        username = username.lower().strip()
        client_ip = get_client_ip(request)
        user_agent = get_user_agent(request)
        logger.info(f"Login attempt for user: {username} from IP: {client_ip}")

        # Check if account is locked due to too many failed attempts
        is_locked, remaining_minutes = await crud_login_attempt.is_account_locked(db, username)
        if is_locked:
            logger.warning(f"Login blocked for locked account: {username}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Account temporarily locked due to too many failed login attempts. Please try again in {remaining_minutes} minute(s)."
            )

        # Authenticate user
        user = await authenticate_user(
            db,
            username,
            password
        )

        if not user:
            # Record failed attempt (increments counter or creates new record)
            await crud_login_attempt.record_failed_attempt(
                db,
                username=username,
                ip_address=client_ip
            )

            # Get remaining attempts after recording the failure
            remaining_attempts = await crud_login_attempt.get_remaining_attempts(db, username)
            logger.warning(f"Login failed for user: {username}. Remaining attempts: {remaining_attempts}")

            # Check if this attempt triggered a lockout
            if remaining_attempts <= 0:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Account locked due to too many failed login attempts. Please try again in {settings.LOGIN_LOCKOUT_MINUTES} minute(s)."
                )

            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid username/email or password. {remaining_attempts} attempt(s) remaining."
            )

        # Clear failed attempts on successful login
        await crud_login_attempt.clear_failed_attempts(db, username)

        # Create tokens and save session to database
        token_data = {
            "sub": user.username,
            "user_id": str(user.user_id),
            "user_name": user.username,
            "role_name": user.role.role_name if user.role else None
        }

        session_data = await create_user_session(
            db=db,
            user=user,
            token_data=token_data,
            ip_address=client_ip,
            user_agent=user_agent
        )

        # Update last login
        await update_last_login(db, user)

        response = TokenResponse(
            access_token=session_data["access_token"],
            refresh_token=session_data["refresh_token"],
            token_type="bearer",
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,  # Convert to seconds
        )

        logger.info(f"Login successful for user: {user.username}")
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during login: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Login failed: {str(e)}"
        )


@router.post("/auth/refresh", response_model=RefreshTokenResponse)
async def refresh_token(
    refresh_request: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db_session)
):
    """
    Refresh access token endpoint

    Generates a new access token using a valid refresh token.
    Updates the session in database with new access token.

    Args:
        refresh_request: Refresh token request
        db: Database session

    Returns:
        RefreshTokenResponse: New access token with expiry info
    """
    try:
        logger.info("Token refresh requested")

        # Refresh session and get new access token
        result = await refresh_user_session(db, refresh_request.refresh_token)

        if not result:
            logger.warning("Token refresh failed - invalid or expired refresh token")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token. Please login again."
            )

        response = RefreshTokenResponse(
            access_token=result["access_token"],
            token_type="bearer",
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60  # Convert to seconds
        )

        logger.info("Token refresh successful")
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during token refresh: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Token refresh failed: {str(e)}"
        )


@router.post("/auth/logout", response_model=LogoutResponse)
async def logout(
    user_and_token: tuple = Depends(get_current_user_with_token),
    db: AsyncSession = Depends(get_db_session)
):
    """
    User logout endpoint - deletes the session from database.

    When ENABLE_TOKEN_BLACKLIST is True, the session is deleted from DB,
    which invalidates both access and refresh tokens.

    Args:
        user_and_token: Tuple of (current_user, token) from dependency
        db: Database session

    Returns:
        LogoutResponse: Logout confirmation
    """
    try:
        current_user, token = user_and_token
        logger.info(f"Logout requested for user: {current_user.username}")

        # Delete session from database (invalidates both access and refresh tokens)
        if settings.ENABLE_TOKEN_BLACKLIST:
            session_deleted = await invalidate_session(db, token)
            if session_deleted:
                logger.info(f"Session deleted for user: {current_user.username}")
            else:
                logger.warning(f"No session found to delete for user: {current_user.username}")

        response = LogoutResponse(
            message=f"User {current_user.username} logged out successfully"
        )

        logger.info(f"User logged out successfully: {current_user.username}")
        return response

    except Exception as e:
        logger.error(f"Error during logout: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Logout failed"
        )


@router.post("/auth/unlock-account/{username}", status_code=status.HTTP_200_OK)
async def unlock_account(
    username: str,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """
    Unlock a user account (Admin only)

    Clears all failed login attempts for the specified username,
    effectively unlocking the account if it was locked.

    Args:
        username: The username to unlock
        db: Database session
        current_user: Current authenticated admin user

    Returns:
        Success message
    """
    try:
        username = username.lower().strip()
        logger.info(f"Admin {current_user.username} requesting to unlock account: {username}")

        # Check if account is actually locked
        is_locked, _ = await crud_login_attempt.is_account_locked(db, username)

        # Clear failed attempts
        cleared = await crud_login_attempt.clear_failed_attempts(db, username)

        if is_locked:
            logger.info(f"Account {username} unlocked by admin {current_user.username}")
            return {
                "message": f"Account '{username}' has been unlocked successfully.",
                "was_locked": True
            }
        else:
            logger.info(f"Account {username} was not locked. Cleared attempts: {cleared}")
            return {
                "message": f"Account '{username}' was not locked. Failed attempts cleared.",
                "was_locked": False
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unlocking account: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to unlock account: {str(e)}"
        )


@router.get("/auth/account-status/{username}", status_code=status.HTTP_200_OK)
async def get_account_status(
    username: str,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """
    Get account lockout status (Admin only)

    Returns the current lockout status for the specified username.

    Args:
        username: The username to check
        db: Database session
        current_user: Current authenticated admin user

    Returns:
        Account lockout status information
    """
    try:
        username = username.lower().strip()
        logger.info(f"Admin {current_user.username} checking account status for: {username}")

        is_locked, remaining_minutes = await crud_login_attempt.is_account_locked(db, username)
        failed_attempts = await crud_login_attempt.get_failed_attempts_count(db, username)
        remaining_attempts = await crud_login_attempt.get_remaining_attempts(db, username)

        return {
            "username": username,
            "is_locked": is_locked,
            "remaining_lockout_minutes": remaining_minutes,
            "failed_attempts_count": failed_attempts,
            "remaining_attempts": remaining_attempts,
            "max_attempts": settings.MAX_LOGIN_ATTEMPTS,
            "lockout_duration_minutes": settings.LOGIN_LOCKOUT_MINUTES
        }

    except Exception as e:
        logger.error(f"Error getting account status: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get account status: {str(e)}"
        )


@router.post("/auth/cleanup-expired-sessions", status_code=status.HTTP_200_OK)
async def cleanup_expired_sessions(
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """
    Cleanup expired sessions from database (Admin only)

    Removes sessions where refresh token has expired.
    Should be run periodically or on-demand.

    Args:
        db: Database session
        current_user: Current authenticated admin user

    Returns:
        Number of expired sessions removed
    """
    try:
        logger.info(f"Admin {current_user.username} initiated session cleanup")

        removed_count = await crud_user_session.cleanup_expired_sessions(db)

        logger.info(f"Cleaned up {removed_count} expired sessions")
        return {
            "message": f"Successfully cleaned up {removed_count} expired session(s)",
            "removed_count": removed_count
        }

    except Exception as e:
        logger.error(f"Error cleaning up expired sessions: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cleanup expired sessions: {str(e)}"
        )
