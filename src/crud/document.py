import uuid
from typing import List, Optional
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import defer
from sqlalchemy import and_, delete, desc, func, update

from ..models.document import Document
from ..core.database import sessionmanager

STATES = ["NOT_STARTED", "IN_PROGRESS", "COMPLETED", "FAILED"]

class CRUDDocument:
    """
    CRUD methods for the Document model.
    """

    async def get_by_state_center_and_department(
        self,   
        db: AsyncSession,     
        state_center_id: str, 
        original_filename: str,
        department_id: Optional[str]
    ) -> Optional[Document]:
        
        conditions = [
            Document.state_center_id == state_center_id,
            Document.filename == original_filename
        ]
        if department_id:
            conditions.append(Document.department_id == department_id)
        else:
            conditions.append(Document.department_id.is_(None))

        stmt = select(Document).where(and_(*conditions)).limit(1)        
        result = await db.execute(stmt)
        return result.scalars().one_or_none()

    async def get_documents(
        self,
        db: AsyncSession,
        summary_status: str | None = None,
        state_center_id: int | None = None,
        department_id: int | None = None,
        filename: str | None = None,
        document_name: str | None = None,
        uploader_id: int | None = None,
        include_summary: bool = False,
        skip: int = 0,
        limit: int = 20,
    ):
        filters = []

        # Filter: summary_status
        if summary_status:
            if summary_status not in STATES:
                raise HTTPException(status_code=400, detail="Invalid summary_status filter")
            filters.append(Document.summary_status == summary_status)

        # Other filters
        if state_center_id:
            filters.append(Document.state_center_id == state_center_id)

        if department_id:
            filters.append(Document.department_id == department_id)

        if filename:
            filters.append(Document.filename == filename)

        if document_name:
            filters.append(Document.document_name == document_name)

        if uploader_id:
            filters.append(Document.uploader_id == uploader_id)

        # ----- Total count -----
        total_query = select(func.count(Document.file_id)).filter(*filters)
        total = (await db.execute(total_query)).scalar()

        # ----- Base select -----
        doc_query = select(Document).filter(*filters)

        # Ordering, pagination
        doc_query = (
            doc_query.order_by(Document.created_at.desc())
            .offset(skip)
            .limit(limit)
        )

        docs = (await db.execute(doc_query)).scalars().all()

        return total, docs

    async def get_by_identifiers(
        self, 
        db: AsyncSession, 
        identifiers: List[uuid.UUID]
    ) -> Optional[List[Document]]:
        
        if not identifiers:
            return []

        stmt = select(Document).filter(
            Document.file_id.in_(identifiers)
        )
        result = await db.execute(stmt)
        return result.scalars().all()

    async def get_by_id(self, document_id: uuid.UUID) -> Optional[Document]:
        stmt = select(Document).filter(Document.file_id == document_id)
        async with sessionmanager.session() as db:
            result = await db.execute(stmt)
            return result.scalars().first()
        
    async def create(self, db: AsyncSession, db_obj: Document) -> Document:
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
    ) -> Document:
        
        stmt = update(Document).where(Document.file_id == record_id).values(**update_records).returning(Document)
        async with sessionmanager.session() as db:
            result = await db.execute(stmt)
            await db.commit()
            return result.scalar_one()

    async def delete_by_id(self, session: AsyncSession, file_id: uuid.UUID) -> bool:
        """Delete a UserAddedCourse record by ID, ensuring it belongs to the user."""
        stmt = (
            delete(Document)
            .where(Document.file_id == file_id)
        )
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount > 0
    
# Initialize the CRUD utility for use across the application
crud_document = CRUDDocument()