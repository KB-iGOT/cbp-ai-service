import uuid
from typing import Optional, List
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

# Import model and schemas
from ..models.course_suggestion import SuggestedCourse

class CRUDSuggestedCourse:
    """
    CRUD methods for the SuggestedCourse model, supporting asynchronous operations.
    """
    
    async def get_by_role_mapping_and_user(
        self, 
        db: AsyncSession, 
        role_mapping_id: uuid.UUID, 
        user_id: uuid.UUID
    ) -> Optional[SuggestedCourse]:
        """
        Retrieves the SuggestedCourse record associated with a specific 
        role mapping ID and user ID.
        
        Args:
            db: The async database session.
            role_mapping_id: The ID of the RoleMapping to filter by.
            user_id: The ID of the user.
            
        Returns:
            The matching SuggestedCourse object, or None.
        """
        stmt = select(SuggestedCourse).filter(
            SuggestedCourse.role_mapping_id == role_mapping_id,
            SuggestedCourse.user_id == user_id
        ).limit(1)
        
        result = await db.execute(stmt)
        # Use scalars().one_or_none() for single-record retrieval
        return result.scalars().one_or_none()

    async def create(
        self, 
        db: AsyncSession, 
        user_id: uuid.UUID,
        role_mapping_id: uuid.UUID, 
        course_identifiers: List[str]
    ) -> SuggestedCourse:
        """
        Creates a new SuggestedCourse record.
        """
        db_obj = SuggestedCourse(
            user_id=user_id,
            role_mapping_id=role_mapping_id,
            course_identifiers=course_identifiers
        )

        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        
        return db_obj

    async def update(
        self, 
        db: AsyncSession, 
        course_suggested_id: uuid.UUID, 
        update_records
    ) -> SuggestedCourse:
        """
        Updates the course_identifiers list for an existing SuggestedCourse record.
        
        Args:
            db_obj: The existing ORM object to update.
            obj_in: Pydantic schema containing the new list of course identifiers.
            
        Returns:
            The updated SuggestedCourse object.
        """
        stmt = update(SuggestedCourse).where(SuggestedCourse.id == course_suggested_id).values(**update_records).returning(SuggestedCourse)
        result = await db.execute(stmt)
        await db.commit()
        return result.scalar_one()


    async def delete_by_role_mapping_and_user(
        self, 
        db: AsyncSession, 
        role_mapping_id: uuid.UUID, 
        user_id: uuid.UUID
    ) -> bool:
        """
        Deletes a SuggestedCourse record based on role_mapping_id and user_id.

        Returns:
            True if a record was found and deleted, False otherwise.
        """
        # Find the record first
        db_obj = await self.get_by_role_mapping_and_user(db, role_mapping_id, user_id)
        
        if db_obj:
            await db.delete(db_obj)
            await db.commit()
            return True
        return False

# Initialize the CRUD utility for use across the application
crud_suggested_course = CRUDSuggestedCourse()