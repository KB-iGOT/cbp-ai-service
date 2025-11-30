from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from ..core.database import get_db_session
from ..core.security import get_user_from_token
from ..models.user import User

from ..core.logger import logger

# OAuth2PasswordBearer scheme - this will automatically add the /auth/login endpoint to OpenAPI docs
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/cbp-tpc-ai/api/v1/auth/login",  # URL where clients can get tokens
    scheme_name="JWT",
    auto_error=False  # We'll handle errors manually for better control
)

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db_session)
) -> User:
    """
    Dependency to get current authenticated user from JWT token
    
    Args:
        token: JWT token from OAuth2PasswordBearer
        db: Database session
        
    Returns:
        User: Current authenticated user
        
    Raises:
        HTTPException: If authentication fails
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        if not token:
            logger.warning("No token provided")
            raise credentials_exception
        
        # Get user from token
        user = await get_user_from_token(db, token)
        if not user:
            logger.warning("Invalid or expired token")
            raise credentials_exception
        
        logger.debug(f"User authenticated successfully: {user.username}")
        return user
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_current_user dependency: {str(e)}")
        raise credentials_exception

async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """
    Dependency to get current active user
    
    Args:
        current_user: Current authenticated user
        
    Returns:
        User: Current active user
        
    Raises:
        HTTPException: If user is inactive
    """
    try:
        if not current_user.is_active:
            logger.warning(f"Inactive user attempted access: {current_user.username}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Inactive user account"
            )
        
        logger.debug(f"Active user verified: {current_user.username}")
        return current_user
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_current_active_user dependency: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User verification failed"
        )

def require_permissions(required_permissions: list):
    """
    Dependency factory for permission-based access control
    
    Args:
        required_permissions: List of required permissions
        
    Returns:
        Dependency function that checks user permissions
    """
    def permission_checker(current_user: User = Depends(get_current_active_user)) -> User:
        try:
            user_permissions = current_user.role.permissions if current_user.role else {}
            
            # Check if user has all required permissions
            for permission in required_permissions:
                if not user_permissions.get(permission, False):
                    logger.warning(
                        f"User {current_user.username} lacks required permission: {permission}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Insufficient permissions. Required: {permission}"
                    )
            
            logger.debug(f"Permission check passed for user: {current_user.username}")
            return current_user
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in permission checker: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permission check failed"
            )
    
    return permission_checker

def require_role(required_role: str):
    """
    Dependency factory for role-based access control
    
    Args:
        required_role: Required role name
        
    Returns:
        Dependency function that checks user role
    """
    async def role_checker(current_user: User = Depends(get_current_active_user)) -> User:
        try:
            if not current_user.role or current_user.role.role_name != required_role:
                logger.warning(
                    f"User {current_user.username} lacks required role: {required_role}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Insufficient role. Required: {required_role}"
                )
            
            logger.debug(f"Role check passed for user: {current_user.username}")
            return current_user
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in role checker: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Role check failed"
            )
    
    return role_checker