import uuid
import math
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from sqlalchemy import and_, desc, func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from ..models.approval_request import ApprovalRequest, ApprovalRequestItem
from ..schemas.comman import ApprovalStatus


class CRUDApprovalRequest:
    """
    CRUD methods for ApprovalRequest and ApprovalRequestItem models.
    """

    async def create_approval_request(
        self,
        db: AsyncSession,
        request_name: str,
        user_id: uuid.UUID,
        state_center_id: str,
        department_id: Optional[str],
        mdo_id: str,
        items: List[ApprovalRequestItem],
        state_center_name: str = None,
        department_name: str = None,
        org_type: str = None,
    ) -> ApprovalRequest:
        """
        Create a new approval request with its items (snapshots).
        """
        approval_request = ApprovalRequest(
            request_name=request_name,
            user_id=user_id,
            state_center_id=state_center_id,
            department_id=department_id,
            state_center_name=state_center_name,
            department_name=department_name,
            org_type=org_type,
            mdo_id=mdo_id,
            designation_count=len(items),
            status=ApprovalStatus.PENDING,
            items=items
        )

        db.add(approval_request)
        await db.commit()
        await db.refresh(approval_request)
        return approval_request

    async def get_by_request_id(
        self,
        db: AsyncSession,
        request_id: uuid.UUID,
        user_id: Optional[uuid.UUID] = None
    ) -> Optional[ApprovalRequest]:
        """
        Get an approval request by its UUID.
        Optionally filter by user_id for ownership check.
        """
        conditions = [ApprovalRequest.id == request_id]
        if user_id is not None:
            conditions.append(ApprovalRequest.user_id == user_id)

        stmt = (
            select(ApprovalRequest)
            .options(selectinload(ApprovalRequest.items))
            .where(and_(*conditions))
        )
        result = await db.execute(stmt)
        return result.scalars().first()

    async def list_requests(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        page: int = 1,
        page_size: int = 10,
        search: Optional[ApprovalStatus] = None,
        status_filter: Optional[str] = None
    ) -> Tuple[List[ApprovalRequest], int]:
        """
        List approval requests for a user with pagination, search, and status filter.
        Returns (items, total_count).
        """
        conditions = [ApprovalRequest.user_id == user_id]

        # Search: partial match on request_name
        if search:
            search_term = search.strip()
            conditions.append(
                or_(
                    ApprovalRequest.request_name.ilike(f"%{search_term}%")
                )
            )

        # Status filter
        if status_filter:
            try:
                status_enum = ApprovalStatus(status_filter)
                conditions.append(ApprovalRequest.status == status_enum)
            except ValueError:
                pass  # Invalid status filter, ignore

        where_clause = and_(*conditions)

        # Count total
        count_stmt = select(func.count()).select_from(
            ApprovalRequest).where(where_clause)
        count_result = await db.execute(count_stmt)
        total = count_result.scalar_one()

        # Fetch page
        offset = (page - 1) * page_size
        stmt = (
            select(ApprovalRequest)
            .where(where_clause)
            .order_by(desc(ApprovalRequest.created_at))
            .offset(offset)
            .limit(page_size)
        )
        result = await db.execute(stmt)
        items = list(result.scalars().all())

        return items, total

    async def revoke_request(
        self,
        db: AsyncSession,
        request_id: uuid.UUID,
        user_id: uuid.UUID
    ) -> Optional[ApprovalRequest]:
        """
        Revoke a pending approval request. Only allowed if status is PENDING.
        Returns the updated request or None if not found/not allowed.
        """
        # Fetch the request
        approval = await self.get_by_request_id(db, request_id, user_id)
        if not approval:
            return None

        if approval.status != ApprovalStatus.PENDING:
            return None  # Can only revoke pending requests

        stmt = (
            update(ApprovalRequest)
            .where(
                and_(
                    ApprovalRequest.id == request_id,
                    ApprovalRequest.user_id == user_id,
                    ApprovalRequest.status == ApprovalStatus.PENDING
                )
            )
            .values(
                status=ApprovalStatus.DRAFT,
                revoked_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
        )
        await db.execute(stmt)
        await db.commit()

        # Re-fetch updated record
        return await self.get_by_request_id(db, request_id, user_id)


crud_approval_request = CRUDApprovalRequest()
