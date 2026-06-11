import uuid
from datetime import datetime, time as dtime
from typing import List, Optional

from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.designation_approval import DesignationApproval, DesignationApprovalStatus


class CRUDDesignationApproval:

    async def check_duplicate(
        self,
        db: AsyncSession,
        rolemapping_id: uuid.UUID,
        user_id: uuid.UUID,
        designation_name: str,
        wing_division_section: str,
    ) -> Optional[DesignationApproval]:
        """Check if an identical request already exists."""
        stmt = select(DesignationApproval).where(
            and_(
                DesignationApproval.rolemapping_id == rolemapping_id,
                DesignationApproval.user_id == user_id,
                DesignationApproval.designation_name == designation_name,
                DesignationApproval.wing_division_section == wing_division_section,
                DesignationApproval.status != DesignationApprovalStatus.REJECTED.value
            )
        )
        result = await db.execute(stmt)
        return result.scalars().first()

    async def create(
        self,
        db: AsyncSession,
        rolemapping_id: uuid.UUID,
        user_id: uuid.UUID,
        designation_name: str,
        wing_division_section: str,
    ) -> DesignationApproval:
        """Create a new designation approval request."""
        record = DesignationApproval(
            rolemapping_id=rolemapping_id,
            user_id=user_id,
            designation_name=designation_name,
            wing_division_section=wing_division_section,
            status=DesignationApprovalStatus.PENDING.value,
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return record

    async def search(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        designation_name: Optional[str] = None,
        status: Optional[str] = None,
        from_date=None,
        to_date=None,
        page: int = 1,
        page_size: int = 10,
    ) -> tuple[List[DesignationApproval], int]:
        """Search designation approvals with filters and pagination. Returns (records, total_count)."""
        conditions = [DesignationApproval.user_id == user_id]

        if designation_name:
            conditions.append(
                DesignationApproval.designation_name.ilike(f"%{designation_name}%")
            )

        if status:
            conditions.append(DesignationApproval.status == status)

        if from_date:
            conditions.append(
                DesignationApproval.created_at >= datetime.combine(from_date, dtime.min)
            )

        if to_date:
            conditions.append(
                DesignationApproval.created_at <= datetime.combine(to_date, dtime.max)
            )

        where_clause = and_(*conditions)

        # Total count
        count_stmt = select(func.count()).select_from(DesignationApproval).where(where_clause)
        total = (await db.execute(count_stmt)).scalar() or 0

        # Paginated results
        offset = (page - 1) * page_size
        stmt = (
            select(DesignationApproval)
            .where(where_clause)
            .order_by(desc(DesignationApproval.created_at))
            .offset(offset)
            .limit(page_size)
        )
        result = await db.execute(stmt)
        records = result.scalars().all()

        return records, total


crud_designation_approval = CRUDDesignationApproval()
