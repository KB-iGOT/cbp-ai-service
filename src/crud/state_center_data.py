import uuid
from typing import Optional, List, Union, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_, update

# Import model and schemas
from ..models.state_center_data import StateCenterData
from ..core.database import sessionmanager

class CRUDStateCenterData:
    """
    CRUD methods for the StateCenterData model, supporting asynchronous operations.
    """

    async def get_by_id(self, record_id: uuid.UUID) -> Optional[StateCenterData]:
        """
        Retrieve a single StateCenterData record by its primary key ID asynchronously.
        
        Refactored from synchronous db.query().filter().first() to 
        async await db.execute(select().filter()).scalars().first().
        """
        # Construct the SQLAlchemy 2.0 select statement
        stmt = select(StateCenterData).filter(StateCenterData.id == record_id)
        async with sessionmanager.session() as db:
            # Execute the statement asynchronously
            result = await db.execute(stmt)
            
            # Extract the single result
            return result.scalars().first()

    async def get_by_state_center_and_department(
        self,        
        state_center_id: str, 
        department_id: Optional[str]
    ) -> Optional[StateCenterData]:
        """
        Retrieves a single StateCenterData record based on state_center_id and 
        conditionally on department_id.
        
        If department_id is provided, it matches that department.
        If department_id is None, it matches records where department_id is NULL.
        
        Args:
            db: The async database session.
            state_center_id: The ID of the state center.
            department_id: Optional ID of the department.
            
        Returns:
            The matching StateCenterData object, or None.
        """
        
        # Start with the mandatory filter
        conditions = [StateCenterData.state_center_id == state_center_id]
        
        # Apply conditional department filter (matching your request logic)
        if department_id:
            conditions.append(StateCenterData.department_id == department_id)
        else:
            # If department_id is None, we explicitly search for records where the column is NULL
            conditions.append(StateCenterData.department_id.is_(None))

        # Build the statement using sqlalchemy.future.select and sqlalchemy.and_
        stmt = select(StateCenterData).where(and_(*conditions)).limit(1)        
        async with sessionmanager.session() as db:
            result = await db.execute(stmt)
            # Use scalars().one_or_none() for single-record retrieval
            return result.scalars().one_or_none()

    async def create(
        self, 
        db: AsyncSession,
        db_obj: StateCenterData
    ) -> StateCenterData:
        """
        Creates a new StateCenterData record.
        """
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        
        return db_obj

    async def update(
        self, 
        record_id: uuid.UUID, 
        update_records
    ) -> StateCenterData:
        
        stmt = update(StateCenterData).where(StateCenterData.id == record_id).values(**update_records).returning(StateCenterData)
        async with sessionmanager.session() as db:
            result = await db.execute(stmt)
            await db.commit()
            updated_record = result.scalar_one()
            return updated_record

    async def delete(
        self, 
        db: AsyncSession, 
        db_obj: StateCenterData
    ) -> None:
        """
        Deletes a specific StateCenterData record instance.
        """
        await db.delete(db_obj)
        await db.commit()

# Initialize the CRUD utility for use across the application
crud_state_center_data = CRUDStateCenterData()