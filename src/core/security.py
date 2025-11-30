from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from pwdlib import PasswordHash

from ..models.user import User
from ..core.configs import settings
from ..core.logger import logger
from ..crud.user import crud_user
        
# JWT configuration
SECRET_KEY = settings.SECRET_KEY
ALGORITHM = settings.ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES
REFRESH_TOKEN_EXPIRE_DAYS = settings.REFRESH_TOKEN_EXPIRE_DAYS

password_hash = PasswordHash.recommended()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    try:
        result = password_hash.verify(plain_password, hashed_password)
        logger.debug("Password verification completed")
        return result
    except Exception as e:
        logger.error(f"Error verifying password: {str(e)}")
        return False

def get_password_hash(password: str) -> str:
    """Generate password hash"""
    try:
        hash_result = password_hash.hash(password)
        logger.debug("Password hash generated successfully")
        return hash_result
    except Exception as e:
        logger.error(f"Error generating password hash: {str(e)}")
        raise Exception(f"Password hashing failed: {str(e)}")

async def authenticate_user(db: AsyncSession, username_or_email: str, password: str) -> Optional[User]:
    """Authenticate user by username/email and password"""
    try:
        logger.info(f"Attempting to authenticate user: {username_or_email}")
        
        # Try to find user by username or email
        user = await crud_user.get_by_username(db, username_or_email)
        
        if not user:
            logger.warning(f"User not found: {username_or_email}")
            return None
        
        if not user.is_active:
            logger.warning(f"User account is inactive: {username_or_email}")
            return None
        
        # Verify password
        if not verify_password(password, user.password_hash):
            logger.warning(f"Invalid password for user: {username_or_email}")
            return None
        
        logger.info(f"User authenticated successfully: {user.username}")
        return user
        
    except Exception as e:
        logger.error(f"Error authenticating user: {str(e)}")
        return None

def create_access_token(data: dict) -> str:
    """Create JWT access token"""
    try:
        to_encode = data.copy()
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        to_encode.update({"exp": expire, "type": "access"})
        
        encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
        logger.debug("Access token created successfully")
        return encoded_jwt
        
    except Exception as e:
        logger.error(f"Error creating access token: {str(e)}")
        raise Exception(f"Access token creation failed: {str(e)}")

def create_refresh_token(data: dict) -> str:
    """Create JWT refresh token"""
    try:
        to_encode = data.copy()
        expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
        to_encode.update({"exp": expire, "type": "refresh"})
        
        encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
        logger.debug("Refresh token created successfully")
        return encoded_jwt
        
    except Exception as e:
        logger.error(f"Error creating refresh token: {str(e)}")
        raise Exception(f"Refresh token creation failed: {str(e)}")

def verify_token(token: str, token_type: str = "access") -> Optional[dict]:
    """Verify and decode JWT token"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        
        # Check token type
        if payload.get("type") != token_type:
            logger.info(f"Invalid token type. Expected: {token_type}, Got: {payload.get('type')}")
            return None
        
        logger.debug(f"{token_type.capitalize()} token verified successfully")
        return payload
        
    except ExpiredSignatureError:
        logger.error("Token has expired")
        return None
    except JWTError as e:
        logger.error(f"JWT error: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error verifying token: {str(e)}")
        return None

async def get_user_from_token(db: AsyncSession, token: str) -> Optional[User]:
    """Get user from access token"""
    try:
        payload = verify_token(token, "access")
        if not payload:
            return None
        
        username = payload.get("sub")
        if not username:
            logger.warning("Token payload missing subject")
            return None
        
        user = await crud_user.get_by_username(db, username)
        if not user:
            logger.warning(f"User not found from token: {username}")
            return None
        
        if not user.is_active:
            logger.warning(f"User account is inactive: {username}")
            return None
        
        logger.debug(f"User retrieved from token successfully: {user.username}")
        return user
        
    except Exception as e:
        logger.error(f"Error getting user from token: {str(e)}")
        return None

def refresh_access_token(refresh_token: str) -> Optional[str]:
    """Generate new access token from refresh token"""
    try:
        payload = verify_token(refresh_token, "refresh")
        if not payload:
            logger.warning("Invalid refresh token")
            return None
        
        username = payload.get("sub")
        if not username:
            logger.warning("Refresh token payload missing subject")
            return None
        
        # Create new access token
        access_token_data = {"sub": username}
        new_access_token = create_access_token(access_token_data)
        
        logger.info(f"Access token refreshed successfully for user: {username}")
        return new_access_token
        
    except Exception as e:
        logger.error(f"Error refreshing access token: {str(e)}")
        return None

async def update_last_login(db: AsyncSession, user: User) -> None:
    """Update user's last login timestamp"""
    try:
        await crud_user.update_last_login(db, user)
        logger.debug(f"Last login updated for user: {user.username}")
    except Exception as e:
        logger.error(f"Error updating last login: {str(e)}")
        await db.rollback()