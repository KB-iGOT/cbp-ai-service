import uuid
from typing import List, Optional
from sqlalchemy import and_, delete, desc, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

# Assuming RoleMapping is defined in src/models/cbp_plan.py
from ..models.role_mapping import ProcessingStatus, RoleMapping 
from ..core.database import sessionmanager 

class CRUDRoleMapping:
    """
    CRUD methods for the RoleMapping model.
    """
    
    async def _get_by_id_in_session(self, db: AsyncSession, role_mapping_id: uuid.UUID) -> Optional[RoleMapping]:
        """Internal method to retrieve a record using an injected session."""
        stmt = select(RoleMapping).filter(RoleMapping.id == role_mapping_id)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_by_id(self, role_mapping_id: uuid.UUID) -> Optional[RoleMapping]:
        """
        Retrieves a RoleMapping record by its primary key ID, managing its own session.
        """
        async with sessionmanager.session() as db:
            return await self._get_by_id_in_session(db, role_mapping_id)

    async def get_by_id_and_user(
        self, 
        db: AsyncSession, 
        role_mapping_id: uuid.UUID, 
        user_id: uuid.UUID
    ) -> Optional[RoleMapping]:
        """
        Retrieves a RoleMapping record filtered by its ID and the associated user ID.

        Args:
            db: The async database session.
            role_mapping_id: The ID of the role mapping.
            user_id: The ID of the current user.

        Returns:
            The matching RoleMapping object or None if not found.
        """
        # Construct the SQLAlchemy 2.0 style select statement
        stmt = select(RoleMapping).filter(
            RoleMapping.id == role_mapping_id,
            RoleMapping.user_id == user_id
        )
        
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_all_mapping(
        self, 
        db: AsyncSession, 
        state_center_id: str, 
        user_id: uuid.UUID,
        department_id: Optional[str]
    ) -> Optional[RoleMapping]:
        """
        Checks for an existing RoleMapping record based on state_center_id, user_id, 
        and department_id (or department_id is NULL).

        Args:
            db: The async database session.
            state_center_id: The ID of the state center.
            user_id: The ID of the user (current_user.user_id).
            department_id: Optional ID of the department.
            
        Returns:
            The matching RoleMapping object, or None if not found.
        """
        
        conditions = [
            RoleMapping.state_center_id == state_center_id,
            RoleMapping.user_id == user_id
        ]
        
        # Apply conditional department filter (matching your request logic)
        if department_id:
            conditions.append(RoleMapping.department_id == department_id)
        else:
            # If department_id is None, we explicitly search for records where the column is NULL
            conditions.append(RoleMapping.department_id.is_(None))

        # Build the statement using sqlalchemy.future.select and sqlalchemy.and_
        stmt = select(RoleMapping).where(and_(*conditions)).order_by(desc(RoleMapping.sort_order)).limit(1)
        
        result = await db.execute(stmt)
        # Use scalars().one_or_none() for single-record retrieval
        return result.scalars().one_or_none()
    
    async def get_all_completed_mapping(
        self, 
        db: AsyncSession, 
        state_center_id: str, 
        user_id: uuid.UUID,
        department_id: Optional[str] = None
    ) -> Optional[List[RoleMapping]]:
        """
        Checks for an existing RoleMapping record based on state_center_id, user_id, 
        and department_id (or department_id is NULL).

        Args:
            db: The async database session.
            state_center_id: The ID of the state center.
            user_id: The ID of the user (current_user.user_id).
            department_id: Optional ID of the department.
            
        Returns:
            The matching RoleMapping object, or None if not found.
        """
        
        conditions = [
            RoleMapping.state_center_id == state_center_id,
            RoleMapping.user_id == user_id,
            RoleMapping.status == ProcessingStatus.COMPLETED # Mandatory status filter
        ]
        
        # Apply conditional department filter (matching your request logic)
        if department_id:
            conditions.append(RoleMapping.department_id == department_id)
        else:
            # If department_id is None, we explicitly search for records where the column is NULL
            conditions.append(RoleMapping.department_id.is_(None))

        # Build the statement using sqlalchemy.future.select and sqlalchemy.and_
        stmt = select(RoleMapping).where(and_(*conditions)).order_by(RoleMapping.sort_order)
        
        result = await db.execute(stmt)
        return result.scalars().all()

    async def update(
        self, 
        role_mapping_id: uuid.UUID, 
        update_records
    ) -> RoleMapping:
        
        stmt = (
            update(RoleMapping)
            .where(RoleMapping.id == role_mapping_id)
            .values(**update_records)
            .returning(RoleMapping)
        )
        async with sessionmanager.session() as db:
            result = await db.execute(stmt)
            await db.commit()
            updated_record = result.scalar_one()
            return updated_record
        
    async def create(
        self, 
        new_mappings: List[RoleMapping]
    ) -> List[RoleMapping]:
        async with sessionmanager.session() as db:
            db.add_all(new_mappings)
            await db.commit()
            for mapping in new_mappings:
                await db.refresh(mapping)
            return new_mappings

    async def get_in_progress_mapping(
        self, 
        db: AsyncSession, 
        state_center_id: str, 
        user_id: uuid.UUID,
        department_id: Optional[str]
    ) -> Optional[RoleMapping]:
        """
        Retrieves a RoleMapping record that is currently marked as IN_PROGRESS 
        for the given user and context.
        
        Args:
            db: The async database session.
            state_center_id: The ID of the state center.
            user_id: The ID of the user.
            department_id: Optional ID of the department.
            
        Returns:
            The matching RoleMapping object in IN_PROGRESS status, or None.
        """
        
        conditions = [
            RoleMapping.state_center_id == state_center_id,
            RoleMapping.user_id == user_id,
            RoleMapping.status == ProcessingStatus.IN_PROGRESS # Mandatory status filter
        ]
        
        # Apply conditional department filter
        if department_id:
            conditions.append(RoleMapping.department_id == department_id)
        else:
            # If department_id is None, explicitly search for records where the column is NULL
            conditions.append(RoleMapping.department_id.is_(None))

        stmt = (
            select(RoleMapping)
            .where(and_(*conditions))
            .limit(1) # We only need one match
        )
        
        result = await db.execute(stmt)
        return result.scalars().one_or_none()

    async def delete_existing_mappings(
        self, 
        db: AsyncSession, 
        state_center_id: str, 
        user_id: uuid.UUID,
        department_id: Optional[str]
    ) -> int:
        """
        Deletes all RoleMapping records matching the given user, state center, 
        and department context (or lack thereof). 
        This uses the SQLAlchemy 2.0 style async delete operation.

        Args:
            db: The async database session.
            state_center_id: The ID of the state center.
            user_id: The ID of the user.
            department_id: Optional ID of the department.
            
        Returns:
            The number of rows deleted.
        """
        conditions = [
            RoleMapping.state_center_id == state_center_id,
            RoleMapping.user_id == user_id
        ]

        if department_id:
            conditions.append(RoleMapping.department_id == department_id)
        else:
            conditions.append(RoleMapping.department_id.is_(None))
            
        # Build the delete statement
        stmt = delete(RoleMapping).where(and_(*conditions))
        
        # Execute the statement
        result = await db.execute(stmt)
        
        # Commit the transaction to finalize deletion
        await db.commit()
        
        return result.rowcount
    
    async def delete_by_id(
        self, 
        db: AsyncSession, 
        role_mapping_id: uuid.UUID
    ) -> int:
        conditions = [
            RoleMapping.id == role_mapping_id
        ]

        # Build the delete statement
        stmt = delete(RoleMapping).where(and_(*conditions))
        
        # Execute the statement
        result = await db.execute(stmt)
        
        # Commit the transaction to finalize deletion
        await db.commit()
        
        return result.rowcount
# Initialize the CRUD utility for use across the application
crud_role_mapping = CRUDRoleMapping()