import uuid
from typing import List, Optional
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import defer
from sqlalchemy import and_, delete, desc, func, update

from ..models.meta_summary import MetaSummary
from ..core.database import sessionmanager

STATES = ["NOT_STARTED", "IN_PROGRESS", "COMPLETED", "FAILED"]

class CRUDMetaSummary:
    """
    CRUD methods for the Document model.
    """
    async def get_meta_summaries(
        self,
        db: AsyncSession,
        state_center_id: int | None = None,
        department_id: int | None = None,
        status: str | None = None,
        skip: int = 0,
        limit: int = 20,
    ):
        filters = []

        # Build filters
        if state_center_id:
            filters.append(MetaSummary.state_center_id == state_center_id)

        if department_id:
            filters.append(MetaSummary.department_id == department_id)

        if status:
            valid_statuses = ["PENDING", "IN_PROGRESS", "COMPLETED", "FAILED"]
            if status not in valid_statuses:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid status. Must be one of: {valid_statuses}"
                )
            filters.append(MetaSummary.status == status)

        # ----- Count total -----
        total_query = select(func.count(MetaSummary.id)).filter(*filters)
        total = (await db.execute(total_query)).scalar()

        # ----- Fetch paginated results -----
        data_query = (
            select(MetaSummary)
            .filter(*filters)
            .order_by(desc(MetaSummary.created_at))
            .offset(skip)
            .limit(limit)
        )

        result = await db.execute(data_query)
        meta_summaries = result.scalars().all()
        return total, meta_summaries

    async def get_by_identifiers(
        self, 
        db: AsyncSession, 
        identifiers: List[uuid.UUID]
    ) -> Optional[List[MetaSummary]]:
        
        if not identifiers:
            return []

        stmt = select(MetaSummary).filter(
            MetaSummary.file_ids.contains(identifiers)
        )
        result = await db.execute(stmt)
        return result.scalars().all()

    async def get_by_request_id(self, request_id: uuid.UUID) -> Optional[MetaSummary]:
        stmt = select(MetaSummary).filter(MetaSummary.request_id == request_id)
        async with sessionmanager.session() as db:
            result = await db.execute(stmt)
            return result.scalars().first()
        
    async def create(self, db: AsyncSession, db_obj: MetaSummary) -> MetaSummary:
        """Create a new user record."""
        # The password in obj_in.password must be HASHED before this point.
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj
    
    async def update(
        self, 
        record_id: uuid.UUID, 
        update_records
    ) -> MetaSummary:
        
        stmt = update(MetaSummary).where(MetaSummary.request_id == record_id).values(**update_records).returning(MetaSummary)
        async with sessionmanager.session() as db:
            result = await db.execute(stmt)
            await db.commit()
            return result.scalar_one()

    async def delete_by_id(self, session: AsyncSession, request_id: uuid.UUID) -> bool:
        stmt = (
            delete(MetaSummary)
            .where(MetaSummary.request_id == request_id)
        )
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount > 0
    
# Initialize the CRUD utility for use across the application
crud_meta_summary = CRUDMetaSummary()