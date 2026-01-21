from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...schemas.auth import LogoutResponse, RefreshTokenRequest, RefreshTokenResponse, TokenResponse
from ...models.user import User

from ...core.security import authenticate_user, create_access_token, create_refresh_token, refresh_access_token, update_last_login
from ...core.database import get_db_session
from ...core.configs import settings
from ...core.logger import logger
from ...crud.login_attempt import crud_login_attempt

from ...api.dependencies import get_current_user, require_role


router = APIRouter(tags=["Authentication"])

# Helper function to get client IP
def get_client_ip(request: Request) -> str:
    """Extract client IP from request, handling proxies."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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
    Returns access token and refresh token information.

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
            # Record failed attempt
            remaining_attempts = await crud_login_attempt.get_remaining_attempts(db, username)
            await crud_login_attempt.record_attempt(
                db,
                username=username,
                is_successful=False,
                ip_address=client_ip,
                failure_reason="Invalid credentials"
            )

            logger.warning(f"Login failed for user: {username}. Remaining attempts: {remaining_attempts - 1}")

            # Check if this attempt triggered a lockout
            if remaining_attempts <= 1:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Account locked due to too many failed login attempts. Please try again in {settings.LOGIN_LOCKOUT_MINUTES} minute(s)."
                )

            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid username/email or password. {remaining_attempts - 1} attempt(s) remaining."
            )

        # Record successful login and clear failed attempts
        await crud_login_attempt.record_attempt(
            db,
            username=username,
            is_successful=True,
            ip_address=client_ip
        )
        await crud_login_attempt.clear_failed_attempts(db, username)

        # Create tokens with user details
        token_data = {
            "sub": user.username,
            "user_id": str(user.user_id),
            "user_name": user.username,
            "role_name": user.role.role_name if user.role else None
        }
        access_token = create_access_token(token_data)
        refresh_token = create_refresh_token(token_data)

        # Update last login
        await update_last_login(db, user)

        response = TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
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
    refresh_request: RefreshTokenRequest
):
    """
    Refresh access token endpoint
    
    Generates a new access token using a valid refresh token.
    
    Args:
        refresh_request: Refresh token request
        
    Returns:
        RefreshTokenResponse: New access token with expiry info
    """
    try:
        logger.info("Token refresh requested")
        
        # Generate new access token
        new_access_token = refresh_access_token(refresh_request.refresh_token)
        
        if not new_access_token:
            logger.warning("Token refresh failed - invalid refresh token")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token"
            )
        
        response = RefreshTokenResponse(
            access_token=new_access_token,
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
    current_user: User = Depends(get_current_user)
):
    """
    User logout endpoint
    
    Note: Since we're using stateless JWT tokens, this endpoint primarily 
    serves as a confirmation. In a production environment with token blacklisting,
    you would add the token to a blacklist here.
    
    Args:
        current_user: Current authenticated user
        
    Returns:
        LogoutResponse: Logout confirmation
    """
    try:
        logger.info(f"Logout requested for user: {current_user.username}")
        
        # In a production environment, you might want to:
        # 1. Add the token to a blacklist/cache (Redis)
        # 2. Log the logout event
        # 3. Clear any server-side sessions
        
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
        Success message with cleared attempts count
    """
    try:
        username = username.lower().strip()
        logger.info(f"Admin {current_user.username} requesting to unlock account: {username}")

        # Check if account is actually locked
        is_locked, _ = await crud_login_attempt.is_account_locked(db, username)

        # Clear failed attempts
        cleared_count = await crud_login_attempt.clear_failed_attempts(db, username)

        if is_locked:
            logger.info(f"Account {username} unlocked by admin {current_user.username}. Cleared {cleared_count} failed attempts.")
            return {
                "message": f"Account '{username}' has been unlocked successfully.",
                "cleared_attempts": cleared_count
            }
        else:
            logger.info(f"Account {username} was not locked. Cleared {cleared_count} failed attempts.")
            return {
                "message": f"Account '{username}' was not locked. Cleared {cleared_count} failed attempt(s).",
                "cleared_attempts": cleared_count
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
        failed_attempts = await crud_login_attempt.get_recent_failed_attempts_count(db, username)
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
