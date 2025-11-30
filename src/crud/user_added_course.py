import uuid
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import delete, desc, func, update

from ..schemas.user_added_course import UserAddedCourseUpdate

from ..models.user_added_course import UserAddedCourse

class CRUDUserAddedCourse:
    """
    CRUD methods for the UserAddedCourse model.
    """

    async def get_by_id_and_user(self, session: AsyncSession, role_mapping_id: uuid.UUID, user_id: uuid.UUID) -> Optional[UserAddedCourse]:
        """
        Helper method to check if a RoleMapping exists and belongs to the specified user.
        Crucial for access control before creating/fetching courses.
        """
        stmt = (
            select(UserAddedCourse)
            .where(UserAddedCourse.role_mapping_id == role_mapping_id, UserAddedCourse.user_id == user_id)
            .order_by(desc(UserAddedCourse.created_at))
        )
        result = await session.execute(stmt)
        return result.scalars().first()
    
    async def get_courses_by_id_and_user(self, session: AsyncSession, role_mapping_id: uuid.UUID, user_id: uuid.UUID) -> Optional[List[UserAddedCourse]]:
        """
        Helper method to check if a RoleMapping exists and belongs to the specified user.
        Crucial for access control before creating/fetching courses.
        """
        stmt = (
            select(UserAddedCourse)
            .where(UserAddedCourse.role_mapping_id == role_mapping_id, UserAddedCourse.user_id == user_id)
            .order_by(desc(UserAddedCourse.created_at))
        )
        result = await session.execute(stmt)
        return result.scalars().all()
    
    async def get_by_id(self, session: AsyncSession, course_id: uuid.UUID, user_id: uuid.UUID) -> Optional[UserAddedCourse]:
        """Retrieve a single UserAddedCourse by ID, ensuring it belongs to the user."""
        stmt = select(UserAddedCourse).where(
            UserAddedCourse.id == course_id,
            UserAddedCourse.user_id == user_id
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    async def create(self, db: AsyncSession, db_obj: UserAddedCourse) -> UserAddedCourse:
        """Create a new user record."""
        # The password in obj_in.password must be HASHED before this point.
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj
    
    async def update(self, session: AsyncSession, course_id: uuid.UUID, user_id: uuid.UUID, obj_in: UserAddedCourseUpdate) -> Optional[UserAddedCourse]:
        """Update an existing UserAddedCourse by ID, returning the updated object."""
        update_data = obj_in.model_dump(exclude_unset=True)

        if not update_data:
            # If no data is provided to update, fetch and return the existing object
            return await self.get_by_id(session, course_id, user_id)

        stmt = (
            update(UserAddedCourse)
            .where(UserAddedCourse.id == course_id, UserAddedCourse.user_id == user_id)
            .values(**update_data)
            .returning(UserAddedCourse)
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    
    async def get_user_added_courses_by_identifiers(
        self, 
        db: AsyncSession, 
        user_id: uuid.UUID, 
        role_mapping_id: uuid.UUID, 
        identifiers: List[str]
    ) -> Optional[List[UserAddedCourse]]:
        """
        Retrieves user-added courses filtered by user_id, role_mapping_id, and 
        a list of course identifiers, ordered by creation date descending.

        Args:
            db: The async database session.
            user_id: The ID of the current user.
            role_mapping_id: The ID of the associated role mapping.
            identifiers: A list of course identifier strings (UUIDs) to filter by.

        Returns:
            A list of matching UserAddedCourse objects, or None if no courses are found.
        """
        if not identifiers:
            return []

        stmt = select(UserAddedCourse).filter(
            UserAddedCourse.role_mapping_id == role_mapping_id,
            UserAddedCourse.user_id == user_id,
            UserAddedCourse.identifier.in_(identifiers)
        ).order_by(desc(UserAddedCourse.created_at))
        
        result = await db.execute(stmt)
        return result.scalars().all()

    async def delete_by_id(self, session: AsyncSession, course_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Delete a UserAddedCourse record by ID, ensuring it belongs to the user."""
        stmt = (
            delete(UserAddedCourse)
            .where(UserAddedCourse.id == course_id, UserAddedCourse.user_id == user_id)
        )
        result = await session.execute(stmt)
        await session.commit()
        # rowcount indicates how many rows were affected (deleted)
        return result.rowcount > 0
    
    async def delete_all_by_role_mapping(self, session: AsyncSession, role_mapping_id: uuid.UUID, user_id: uuid.UUID) -> int:
        """
        Delete all UserAddedCourses associated with a specific RoleMapping for the user.
        Returns the number of courses deleted.
        """
        # 1. Count the courses before deleting (mimics the API's need for the deleted count)
        count_stmt = select(func.count(UserAddedCourse.id)).where(
            UserAddedCourse.role_mapping_id == role_mapping_id,
            UserAddedCourse.user_id == user_id
        )
        initial_count = await session.scalar(count_stmt)

        if initial_count == 0:
            return 0

        # 2. Perform the bulk delete
        delete_stmt = (
            delete(UserAddedCourse)
            .where(UserAddedCourse.role_mapping_id == role_mapping_id, UserAddedCourse.user_id == user_id)
        )
        await session.execute(delete_stmt)
        await session.commit()
        return initial_count
    
# Initialize the CRUD utility for use across the application
crud_user_added_course = CRUDUserAddedCourse()