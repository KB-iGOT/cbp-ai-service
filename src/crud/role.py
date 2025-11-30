import uuid
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import desc, func, select

from ..models.role import Role
from ..models.user import User # Used for counting associated users
from ..schemas.role import RoleCreate, RoleUpdate

class CRUDRole:
    """
    Synchronous CRUD operations for the Role model, using SQLAlchemy Session.
    """

    async def get_by_id(self, db: AsyncSession, role_id: uuid.UUID) -> Optional[Role]:
        """Retrieve a role by its UUID."""
        stmt = select(Role).where(
            Role.role_id == role_id
        )
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_by_name(self, db: AsyncSession, role_name: str) -> Optional[Role]:
        """Retrieve a role by its unique name."""
        stmt = select(Role).where(
            Role.role_name == role_name
        )
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_all(
        self, 
        db: AsyncSession, 
        is_active: Optional[bool] = None, 
        skip: int = 0, 
        limit: int = 100
    ) -> List[Role]:
        """Retrieve a list of roles, optionally filtered by active status, ordered by update date."""
        stmt = select(Role)

        if is_active is not None:
            stmt = stmt.where(Role.is_active == is_active)

        stmt = (
            stmt
            .order_by(Role.updated_at.desc())
            .offset(skip)
            .limit(limit)
        )

        result = await db.execute(stmt)
        return result.scalars().all()

    async def count_users_by_role_id(self, db: AsyncSession, role_id: uuid.UUID) -> int:
        """Count the number of users assigned to a specific role ID."""
        stmt = (
            select(func.count())
            .select_from(User)
            .where(User.role_id == role_id)
        )

        result = await db.execute(stmt)
        return result.scalar_one()

    async def create(self, db: AsyncSession, obj_in: RoleCreate) -> Role:
        """Create a new role record."""
        db_obj = Role(
            role_id=uuid.uuid4(),
            role_name=obj_in.role_name,
            description=obj_in.description,
            permissions=obj_in.permissions or {},
            is_active=obj_in.is_active
        )
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def update(self, db: AsyncSession, db_obj: Role, obj_in: RoleUpdate) -> Role:
        """Update an existing role record."""
        # Get only the fields that were provided in the request body
        update_data = obj_in.model_dump(exclude_unset=True)
        
        # Apply updates to the model instance
        for field, value in update_data.items():
            setattr(db_obj, field, value)
            
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def delete(self, db: AsyncSession, db_obj: Role) -> Role:
        """Delete a role record."""
        await db.delete(db_obj)
        await db.commit()
        return db_obj
    
# Instance of the class to be imported in the API routes
crud_role = CRUDRole()