import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.security import get_password_hash
from ...models.user import User
from ...crud.user import crud_user
from ...crud.role import crud_role

from ...schemas.user import PaginatedUserResponse, UserCreate, UserResponse, UserUpdate

from ...api.dependencies import get_current_active_user, require_role
from ...core.database import get_db_session
from ...core.logger import logger

router = APIRouter(tags=["Users"])

# USER MANAGEMENT APIs
@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    user: UserCreate,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """
    Create a new user
    
    Creates a new user with assigned role and optional organizational assignment.
    """
    try:
        logger.info(f"Creating new user: {user.username}")
        
        # Check if username already exists
        existing_user = await crud_user.get_by_username(db,user.username)
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Username/Email already exists"
            )
        
        # Verify role exists
        role = await crud_role.get_by_id(db, user.role_id)
        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role not found"
            )
        
        # Hash password
        password_hash = get_password_hash(user.password)
        
        # Create new user
        db_user = User(
            user_id=uuid.uuid4(),
            username=user.username.lower().strip(),
            email=user.email.lower().strip(),
            phone=user.phone,
            organization_ids=user.organization_ids,
            password_hash=password_hash,
            role_id=user.role_id,
            state_center_id=user.state_center_id,
            department_id=user.department_id,
            is_active=user.is_active,
            created_by=user.created_by
        )
        
        db_user = await crud_user.create(db, db_user)
        
        logger.info(f"Successfully created user with ID: {db_user.user_id}")
        
        # Prepare response with related data
        db_user = await crud_user.get_by_id_with_relations(db, db_user.user_id)
        response = await _prepare_user_response(db_user)
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating user: {str(e)}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create user: {str(e)}"
        )

async def _prepare_user_response(user: User) -> UserResponse:
    """Helper function to prepare user response with related data"""
    
    # Get role info
    role_info = None
    if user.role:
        role_info = {
            "role_id": str(user.role.role_id),
            "role_name": user.role.role_name,
            "description": user.role.description,
            "is_active": user.role.is_active
        }

    # Get creator info
    creator_info = None
    if user.creator:
        creator_info = {
            "user_id": str(user.creator.user_id),
            "username": user.creator.username
        }
    
    return UserResponse(
        user_id=str(user.user_id),
        username=user.username,
        email=user.email,
        phone=user.phone,
        role_id=user.role_id,
        state_center_id=user.state_center_id,
        department_id=user.department_id,
        is_active=user.is_active,
        organization_ids=user.organization_ids,
        last_login=user.last_login,
        created_at=user.created_at,
        updated_at=user.updated_at,
        role_info=role_info,
        creator_info=creator_info
    )

@router.get("/users/me", response_model=UserResponse)
async def get_user(
    db: AsyncSession = Depends(get_db_session),
current_user: User = Depends(get_current_active_user)
):
    """Get a specific user by ID"""
    try:
        user = await crud_user.get_by_id_with_relations(db, current_user.user_id)
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        response = await _prepare_user_response(user)
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching user: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch user"
        )


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """Get a specific user by ID"""
    try:
        user = await crud_user.get_by_id_with_relations(db, user_id)
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        response = await _prepare_user_response(user)
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching user: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch user"
        )

@router.put("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    user_update: UserUpdate,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """Update a user"""
    try:
        user = await crud_user.get_by_id(db, user_id)
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        # Check for duplicate username if being updated
        if user_update.username or user_update.email:
            existing_user = await crud_user.get_by_username(db, user_update.username)
            if existing_user:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Username/Email already exists"
                )
        
        # Verify role exists if being updated
        if user_update.role_id:
            role = await crud_role.get_by_id(db, user_update.role_id)
            if not role:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Role not found"
                )
        
        # Update password if provided
        updated_user = await crud_user.update(db, user.user_id, user_update)
        
        # Prepare response with updated related data
        # updated_user = await crud_user.get_by_id_with_relations(db, user_id)
        
        response = await _prepare_user_response(updated_user)
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating user: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user"
        )

@router.delete("/users/{user_id}")
async def delete_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """Delete a user (soft delete by setting is_active to False)"""
    try:
        user = await crud_user.get_by_id(db, user_id)
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        # Soft delete by setting is_active to False
        await crud_user.update(db, user.user_id, UserUpdate(is_active=False))
        
        return {"message": f"User '{user.username}' deactivated successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting user: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete user"
        )

@router.get("/users", response_model=PaginatedUserResponse)
async def list_users(
    limit: int = 10,
    offset: int = 0,
    # username: str | None = None,
    # email: str | None = None,
    # role_id: uuid.UUID | None = None,
    is_active: bool | None = None,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    try:
        filters = {
            # "username": username,
            # "email": email,
            # "role_id": role_id,
            "is_active": is_active
        }

        total, users = await crud_user.list_users(db, limit, offset, filters)
        users = [await _prepare_user_response(user) for user in users]
        return PaginatedUserResponse(
            total=total,
            limit=limit,
            offset=offset,
            data=users
        )
    except Exception as e:
        # Catch any unexpected error
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to fetch users: {str(e)}"
        )