import uuid
from typing import Any, Dict, Optional, List
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from ..models.cbp_plan import CBPPlan 

class CRUDCBPPlan:
    """
    CRUD methods for the CBPPlan model, supporting asynchronous operations.
    
    Note: The complex logic for calculating 'selected_courses' (fetching 
    recommendations, calling external APIs, etc.) should be implemented in 
    a separate service layer, which then calls these CRUD methods to persist 
    the final data.
    """
    
    async def get_by_id(self, db: AsyncSession, id: uuid.UUID, user_id: uuid.UUID) -> Optional[CBPPlan]:
        """Retrieve a CBP plan by its primary key ID."""
        result = await db.execute(select(CBPPlan).filter(CBPPlan.id == id, CBPPlan.user_id == user_id))
        return result.scalars().first()

    async def get_by_role_mapping(self, db: AsyncSession, role_mapping_id: uuid.UUID, user_id: uuid.UUID) -> Optional[CBPPlan]:
        """Retrieve all CBP plans for a specific role mapping and user."""
        stmt = select(CBPPlan).filter(
            CBPPlan.role_mapping_id == role_mapping_id,
            CBPPlan.user_id == user_id
        ).order_by(CBPPlan.created_at.desc())
        
        result = await db.execute(stmt)
        return result.scalars().first()

    async def create(self, db: AsyncSession, role_mapping_id: uuid.UUID, user_id: uuid.UUID,
                     recommended_course_id:uuid.UUID, selected_courses: List[Dict[str, Any]]) -> CBPPlan:
        """
        Create a new CBP plan record. 
        
        This method assumes the final data structure in obj_in (including the 
        list of selected_courses) is already prepared.
        """
        db_obj = CBPPlan(
            id=uuid.uuid4(),
            user_id=user_id,
            role_mapping_id=role_mapping_id,
            recommended_course_id=recommended_course_id,
            selected_courses=selected_courses # This is the prepared JSON list
        )
        
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def update(
        self, 
        db: AsyncSession,
        record_id: uuid.UUID, 
        update_records
    ) -> CBPPlan:
        
        stmt = update(CBPPlan).where(CBPPlan.id == record_id).values(**update_records).returning(CBPPlan)
        result = await db.execute(stmt)
        await db.commit()
        return result.scalar_one()
            

# Initialize the CRUD utility for use across the application
crud_cbp_plan = CRUDCBPPlan()