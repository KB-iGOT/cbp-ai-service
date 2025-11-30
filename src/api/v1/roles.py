from typing import List, Optional
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import require_role
from ...core.database import get_db_session
from ...crud.role import crud_role

from ...schemas.role import RoleCreate, RoleResponse, RoleUpdate

from ...models.user import User
from ...core.logger import logger

router = APIRouter(tags=["Roles"])

# Role APIs
@router.post("/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    role: RoleCreate,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """
    Create a new role with permissions
    
    Creates a new role in the system with specified permissions.
    Role names must be unique across the system.
    """
    try:
        logger.info(f"Creating new role: {role.role_name}")
        
        # Check if role already exists
        existing_role = await crud_role.get_by_name(db, role.role_name)
        if existing_role:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Role '{role.role_name}' already exists"
            )
        
        db_role = await crud_role.create(db, role)

        logger.info(f"Successfully created role with ID: {db_role.role_id}")
        
        response = RoleResponse(
            role_id=str(db_role.role_id),
            role_name=db_role.role_name,
            description=db_role.description,
            permissions=db_role.permissions or {},
            is_active=db_role.is_active,
            created_at=db_role.created_at,
            updated_at=db_role.updated_at,
            user_count=0
        )
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating role: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create role: {str(e)}"
        )

@router.get("/roles", response_model=List[RoleResponse])
async def get_roles(
    is_active: Optional[bool] = Query(True, description="Filter by active status"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of records"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """
    Get all roles with optional filtering
    
    Retrieves all roles in the system with user counts.
    """
    try:
        logger.info(f"Fetching roles with is_active: {is_active}")
        roles = await crud_role.get_all(db, is_active, skip, limit)
        logger.info(f"Retrieved {len(roles)} roles")
        return roles
        
    except Exception as e:
        logger.error(f"Error fetching roles: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch roles"
        )

@router.get("/roles/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """Get a specific role by ID"""
    try:
        role = await crud_role.get_by_id(db, role_id)
        
        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role not found"
            )
        
        # Count users with this role
        user_count = await crud_role.count_users_by_role_id(db, role_id)
        
        response = RoleResponse(
            role_id=str(role.role_id),
            role_name=role.role_name,
            description=role.description,
            permissions=role.permissions or {},
            is_active=role.is_active,
            created_at=role.created_at,
            updated_at=role.updated_at,
            user_count=user_count
        )
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching role: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch role"
        )

@router.put("/roles/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: uuid.UUID,
    role_update: RoleUpdate,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """Update a role"""
    try:
        role = await crud_role.get_by_id(db, role_id)
        
        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role not found"
            )
        
        # Check for duplicate role name if being updated
        if role_update.role_name and role_update.role_name != role.role_name:
            existing_role = await crud_role.get_by_name(db, role_update.role_name)
            if existing_role:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Role '{role_update.role_name}' already exists"
                )
        
        # Update fields
        role = await crud_role.update(db, role, role_update)
        
        # Get user count
        user_count = await crud_role.count_users_by_role_id(db, role_id)
        
        response = RoleResponse(
            role_id=str(role.role_id),
            role_name=role.role_name,
            description=role.description,
            permissions=role.permissions or {},
            is_active=role.is_active,
            created_at=role.created_at,
            updated_at=role.updated_at,
            user_count=user_count
        )
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating role: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update role"
        )

@router.delete("/roles/{role_id}")
async def delete_role(
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    """Delete a role (only if no users are assigned)"""
    try:
        role = await crud_role.get_by_id(db, role_id)
        
        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role not found"
            )
        
        # Check if any users have this role
        user_count = await crud_role.count_users_by_role_id(db, role_id)
        if user_count > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot delete role: {user_count} users are assigned to this role"
            )
        
        await crud_role.delete(db, role)
        
        return {"message": f"Role '{role.role_name}' deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting role: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete role"
        )
