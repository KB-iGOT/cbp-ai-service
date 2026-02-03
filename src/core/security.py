from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Dict, Any
import uuid as uuid_module
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from pwdlib import PasswordHash

from ..models.user import User
from ..core.configs import settings
from ..core.logger import logger
from ..crud.user import crud_user
from ..crud.user_session import crud_user_session

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


def create_access_token(data: dict) -> Tuple[str, str, datetime]:
    """
    Create JWT access token with unique JTI for session tracking.

    Returns:
        Tuple of (token, jti, expires_at)
    """
    try:
        to_encode = data.copy()
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        jti = str(uuid_module.uuid4())
        to_encode.update({
            "exp": expire,
            "type": "access",
            "jti": jti,
            "iat": datetime.now(timezone.utc)
        })

        encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
        logger.debug(f"Access token created successfully with jti: {jti}")
        return encoded_jwt, jti, expire

    except Exception as e:
        logger.error(f"Error creating access token: {str(e)}")
        raise Exception(f"Access token creation failed: {str(e)}")


def create_refresh_token(data: dict) -> Tuple[str, str, datetime]:
    """
    Create JWT refresh token with unique JTI for session tracking.

    Returns:
        Tuple of (token, jti, expires_at)
    """
    try:
        to_encode = data.copy()
        expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
        jti = str(uuid_module.uuid4())
        to_encode.update({
            "exp": expire,
            "type": "refresh",
            "jti": jti,
            "iat": datetime.now(timezone.utc)
        })

        encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
        logger.debug(f"Refresh token created successfully with jti: {jti}")
        return encoded_jwt, jti, expire

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
        logger.warning("Token has expired")
        return None
    except JWTError as e:
        logger.error(f"JWT error: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error verifying token: {str(e)}")
        return None


def decode_token_payload(token: str) -> Optional[dict]:
    """Decode token payload (used for extracting JTI regardless of expiry)"""
    try:
        # Decode without verification to get payload even if expired
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_exp": False})
        return payload
    except Exception as e:
        logger.error(f"Error decoding token: {str(e)}")
        return None


async def get_user_from_token(db: AsyncSession, token: str) -> Optional[User]:
    """Get user from access token, validating session exists in DB"""
    try:
        payload = verify_token(token, "access")
        if not payload:
            return None

        # Check if session exists (session-based validation)
        jti = payload.get("jti")
        if jti and settings.ENABLE_TOKEN_BLACKLIST:
            is_valid_session = await crud_user_session.is_access_token_valid(db, jti)
            if not is_valid_session:
                logger.warning(f"No active session for token jti: {jti}")
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


async def create_user_session(
    db: AsyncSession,
    user: User,
    token_data: dict,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create tokens and save session to database.

    Args:
        db: Database session
        user: Authenticated user
        token_data: Data to encode in tokens
        ip_address: Client IP
        user_agent: Client user agent

    Returns:
        Dict with access_token, refresh_token, and expiry info
    """
    try:
        # Create tokens
        access_token, access_jti, access_expires = create_access_token(token_data)
        refresh_token, refresh_jti, refresh_expires = create_refresh_token(token_data)

        # Save session to database
        await crud_user_session.create_session(
            db=db,
            user_id=user.user_id,
            access_token=access_token,
            refresh_token=refresh_token,
            access_token_jti=access_jti,
            refresh_token_jti=refresh_jti,
            access_token_expires_at=access_expires,
            refresh_token_expires_at=refresh_expires,
            ip_address=ip_address,
            user_agent=user_agent
        )

        logger.info(f"Session created for user: {user.username}")

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "access_token_expires_at": access_expires,
            "refresh_token_expires_at": refresh_expires
        }

    except Exception as e:
        logger.error(f"Error creating user session: {str(e)}")
        raise


async def refresh_user_session(
    db: AsyncSession,
    refresh_token: str
) -> Optional[Dict[str, Any]]:
    """
    Refresh access token and update session in database.

    Args:
        db: Database session
        refresh_token: Current refresh token

    Returns:
        Dict with new access_token and expiry info, or None if invalid
    """
    try:
        payload = verify_token(refresh_token, "refresh")
        if not payload:
            logger.warning("Invalid refresh token")
            return None

        refresh_jti = payload.get("jti")
        if not refresh_jti:
            logger.warning("Refresh token missing JTI")
            return None

        # Check if session exists
        if settings.ENABLE_TOKEN_BLACKLIST:
            is_valid = await crud_user_session.is_refresh_token_valid(db, refresh_jti)
            if not is_valid:
                logger.warning(f"No active session for refresh token: {refresh_jti}")
                return None

        # Create new access token with same data
        token_data = {
            "sub": payload.get("sub"),
            "user_id": payload.get("user_id"),
            "user_name": payload.get("user_name"),
            "role_name": payload.get("role_name")
        }
        new_access_token, new_access_jti, new_access_expires = create_access_token(token_data)

        # Update session in database
        if settings.ENABLE_TOKEN_BLACKLIST:
            updated_session = await crud_user_session.update_session_access_token(
                db=db,
                refresh_token_jti=refresh_jti,
                new_access_token=new_access_token,
                new_access_token_jti=new_access_jti,
                new_access_token_expires_at=new_access_expires
            )
            if not updated_session:
                logger.warning("Failed to update session")
                return None

        logger.info(f"Access token refreshed for user: {payload.get('sub')}")

        return {
            "access_token": new_access_token,
            "expires_at": new_access_expires
        }

    except Exception as e:
        logger.error(f"Error refreshing user session: {str(e)}")
        return None


async def invalidate_session(db: AsyncSession, token: str) -> bool:
    """
    Invalidate session by deleting it from database (logout).

    Args:
        db: Database session
        token: Access token

    Returns:
        bool: True if session was invalidated
    """
    try:
        payload = decode_token_payload(token)
        if not payload:
            logger.warning("Could not decode token for logout")
            return False

        jti = payload.get("jti")
        if not jti:
            logger.warning("Token missing JTI for logout")
            return False

        deleted = await crud_user_session.delete_session_by_access_token_jti(db, jti)
        if deleted:
            logger.info(f"Session invalidated for jti: {jti}")
        return deleted

    except Exception as e:
        logger.error(f"Error invalidating session: {str(e)}")
        return False


async def invalidate_all_user_sessions(db: AsyncSession, user_id) -> int:
    """
    Invalidate all sessions for a user (logout from all devices).

    Args:
        db: Database session
        user_id: User ID

    Returns:
        int: Number of sessions invalidated
    """
    try:
        count = await crud_user_session.delete_all_user_sessions(db, user_id)
        logger.info(f"Invalidated {count} sessions for user_id: {user_id}")
        return count
    except Exception as e:
        logger.error(f"Error invalidating all user sessions: {str(e)}")
        return 0


async def update_last_login(db: AsyncSession, user: User) -> None:
    """Update user's last login timestamp"""
    try:
        await crud_user.update_last_login(db, user)
        logger.debug(f"Last login updated for user: {user.username}")
    except Exception as e:
        logger.error(f"Error updating last login: {str(e)}")
        await db.rollback()
