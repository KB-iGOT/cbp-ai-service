from fastapi import APIRouter, Depends, Form, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...schemas.auth import LogoutResponse, RefreshTokenRequest, RefreshTokenResponse, TokenResponse
from ...models.user import User

from ...core.security import authenticate_user, create_access_token, create_refresh_token, refresh_access_token, update_last_login
from ...core.database import get_db_session
from ...core.configs import settings
from ...core.logger import logger

from ...api.dependencies import get_current_user


router = APIRouter(tags=["Authentication"])

# Auth APIs
@router.post("/auth/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login(
    username: str = Form(..., min_length=3, max_length=255),
    password: str = Form(..., min_length=8),
    db: AsyncSession = Depends(get_db_session)
):
    """
    User login endpoint
    
    Authenticates user with username/email and password.
    Returns access token and refresh tokeninformation.
    
    Args:
        login_request: Login credentials (username/email and password)
        db: Database session
        
    Returns:
        TokenResponse: Access token, refresh token, and user info
    """
    try:
        username = username.lower().strip()
        logger.info(f"Login attempt for user: {username}")
        
        # Authenticate user
        user = await authenticate_user(
            db, 
            username, 
            password
        )
        
        if not user:
            logger.warning(f"Login failed for user: {username}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username/email or password"
            )
        
        # Create tokens
        token_data = {"sub": user.username}
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
