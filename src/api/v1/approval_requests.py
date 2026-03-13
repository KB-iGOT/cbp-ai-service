import uuid
import math
import logging
from datetime import date, datetime, time
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...core.database import get_db_session
from ...api.dependencies import get_current_active_user
from ...models.user import User
from ...models.role_mapping import RoleMapping, ProcessingStatus
from ...models.approval_request import ApprovalRequestItem
from ...schemas.comman import ApprovalStatus
from ...crud.role_mapping import crud_role_mapping
from ...crud.approval_request import crud_approval_request
from ...schemas.approval_request import (
    SendForApprovalRequest,
    SendForApprovalResponse,
    RevokeApprovalRequest,
    RevokeApprovalResponse,
    ApprovalRequestResponse,
    ApprovalRequestListResponse,
    ApprovalRequestListItem,
    ApprovalRequestItemResponse
)

from sqlalchemy.future import select
from sqlalchemy import and_

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Approval Requests"])

@router.post(
    "/approval-requests/send",
    response_model=SendForApprovalResponse,
    status_code=status.HTTP_201_CREATED
)
async def send_for_approval(
    request: SendForApprovalRequest,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Submit selected role mappings for MDO Admin/Leader approval.
    
    Validates:
    - All role_mapping_ids belong to the current user
    - All role mappings have status = COMPLETED
    - All role mappings have saved CBP plans
    - None are already in a pending/in_review request
    
    Creates a snapshot of role mapping + CBP plan data in approval_request_items.
    """
    try:
        logger.info(
            f"Send for approval: user={current_user.user_id}, "
            f"role_mappings={len(request.role_mapping_ids)}, mdo={request.mdo_id}"
        )

        # ── Step 1: Validate role mappings exist & belong to user ──
        stmt = (
            select(RoleMapping)
            .options(selectinload(RoleMapping.cbp_plans))
            .where(
                and_(
                    RoleMapping.id.in_(request.role_mapping_ids),
                    RoleMapping.user_id == current_user.user_id,
                    RoleMapping.state_center_id == request.state_center_id,
                    RoleMapping.status == ProcessingStatus.COMPLETED
                )
            )
        )
        if request.department_id:
            stmt = stmt.where(RoleMapping.department_id == request.department_id)

        result = await db.execute(stmt)
        role_mappings = list(result.scalars().unique().all())

        if not role_mappings or len(role_mappings) == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No completed role mappings found for the provided IDs."
            )

        # Check all requested IDs were found
        found_ids = {rm.id for rm in role_mappings}
        missing_ids = set(request.role_mapping_ids) - found_ids
        if missing_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Role mappings not found for these IDs: {[str(mid) for mid in missing_ids]}"
            )


        no_cbp = [rm for rm in role_mappings if not rm.cbp_plans or len(rm.cbp_plans) == 0]
        if no_cbp:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Course recommendations not generated/saved for: {[str(rm.id) for rm in no_cbp]}. "
                       f"Please generate & save course recommendation to select."
            )

        # ── Step 6: Create snapshot items ──
        items = []
        for rm in role_mappings:
            item = ApprovalRequestItem(
                source_role_mapping_id=rm.id,
                designation_name=rm.designation_name,
                wing_division_section=getattr(rm, "wing_division_section", None),
                role_responsibilities=getattr(rm, "role_responsibilities", None),
                activities=getattr(rm, "activities", None),
                competencies=getattr(rm, "competencies", None),
                igot_designation_name=rm.igot_designation_name,
                igot_designation_id=rm.igot_designation_id,
                cbp_plan_data=[
                    {
                        "id": str(plan.id),
                        "user_id": str(plan.user_id),
                        "role_mapping_id": str(plan.role_mapping_id),
                        "recommended_course_id": str(plan.recommended_course_id) if plan.recommended_course_id else None,
                        "selected_courses": plan.selected_courses or [],
                        "created_at": plan.created_at.isoformat() if plan.created_at else None,
                        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
                    }
                    for plan in (rm.cbp_plans or [])
                ],
                sort_order=rm.sort_order,
            )
            items.append(item)

        # ── Step 7: Persist ──
        state_center_name = role_mappings[0].state_center_name if role_mappings else None
        department_name = role_mappings[0].department_name if role_mappings else None
        org_type = role_mappings[0].org_type if role_mappings else None
        approval = await crud_approval_request.create_approval_request(
            db=db,
            request_name=request.request_name,
            user_id=current_user.user_id,
            state_center_id=request.state_center_id,
            department_id=request.department_id,
            state_center_name=state_center_name,
            department_name=department_name,
            org_type=org_type,
            mdo_id=request.mdo_id,
            items=items,
        )

        logger.info(f"Approval request created with {len(items)} designations")

        return SendForApprovalResponse(
            id=approval.id,
            status=ApprovalStatus(approval.status) if isinstance(approval.status, str) else approval.status,
            designation_count=approval.designation_count,
            created_at=approval.created_at,
            message="Request successfully submitted for approval."
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error sending for approval")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to submit approval request"
        )

@router.get(
    "/approval-requests",
    response_model=ApprovalRequestListResponse
)
async def list_approval_requests(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    search: Optional[str] = Query(default=None, max_length=200),
    status_filter: Optional[ApprovalStatus] = Query(default=None, alias="status"),
    from_date: Optional[date] = Query(default=None, description="Filter from this date (inclusive, YYYY-MM-DD)"),
    to_date: Optional[date] = Query(default=None, description="Filter up to this date (inclusive, YYYY-MM-DD)"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    List all approval requests submitted by the current user.
    
    - Supports pagination (default 10 per page)
    - Search by request_name (partial match)
    - Filter by status (case-insensitive: draft, pending, published, rejected)
    - Filter by date range (from_date and to_date, inclusive)
    - Default sort: latest first
    """
    try:
        # Validate date range
        if from_date and to_date and from_date > to_date:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="from_date cannot be after to_date"
            )

        # Convert dates to datetime range (start of from_date, end of to_date)
        from_datetime = datetime.combine(from_date, time.min) if from_date else None
        to_datetime = datetime.combine(to_date, time.max) if to_date else None

        items, total = await crud_approval_request.list_requests(
            db=db,
            user_id=current_user.user_id,
            page=page,
            page_size=page_size,
            search=search,
            status_filter=status_filter,
            from_date=from_datetime,
            to_date=to_datetime
        )

        total_pages = math.ceil(total / page_size) if total > 0 else 0

        return ApprovalRequestListResponse(
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            items=[
                ApprovalRequestListItem(
                    id=item.id,
                    request_name=item.request_name,
                    designation_count=item.designation_count,
                    status=item.status,
                    created_at=item.created_at,
                )
                for item in items
            ]
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error listing approval requests")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch approval requests"
        )

@router.get(
    "/approval-requests/{request_id}",
    response_model=ApprovalRequestResponse
)
async def get_approval_request(
    request_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    View full details of a submitted approval request including all designation
    snapshots and course recommendations.
    """
    try:
        approval = await crud_approval_request.get_by_request_id(
            db, request_id, current_user.user_id
        )

        if not approval:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request '{request_id}' not found"
            )

        return ApprovalRequestResponse(
            id=approval.id,
            request_name=approval.request_name,
            user_id=approval.user_id,
            state_center_name=approval.state_center_name,
            department_name=approval.department_name,
            org_type=approval.org_type,
            state_center_id=approval.state_center_id,
            department_id=approval.department_id,
            mdo_id=approval.mdo_id,
            designation_count=approval.designation_count,
            status=approval.status,
            created_at=approval.created_at,
            reviewed_at=approval.reviewed_at,
            revoked_at=approval.revoked_at,
            reviewer_comments=approval.reviewer_comments,
            items=[
                ApprovalRequestItemResponse(
                    id=item.id,
                    source_role_mapping_id=item.source_role_mapping_id,
                    designation_name=item.designation_name,
                    wing_division_section=item.wing_division_section,
                    role_responsibilities=item.role_responsibilities,
                    activities=item.activities,
                    competencies=item.competencies,
                    igot_designation_name=item.igot_designation_name,
                    igot_designation_id=item.igot_designation_id,
                    cbp_plan_data=item.cbp_plan_data,
                    sort_order=item.sort_order
                )
                for item in sorted(approval.items, key=lambda x: x.sort_order or 0)
            ]
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching approval request detail")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch approval request"
        )

@router.post(
    "/approval-requests/revoke",
    response_model=RevokeApprovalResponse
)
async def revoke_approval_request(
    request: RevokeApprovalRequest,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Revoke a pending approval request. 
    
    - Only allowed when status = 'pending'
    - Changes status to 'draft'
    - Cannot revoke approved/rejected requests
    """
    try:
        # First check if it exists
        existing = await crud_approval_request.get_by_request_id(
            db, request.request_id, current_user.user_id
        )

        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request not found"
            )

        if existing.status != ApprovalStatus.PENDING:
            status_val = ApprovalStatus(existing.status)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot revoke request with status '{status_val.value}'. Only 'pending' requests can be revoked."
            )

        # Perform revoke
        revoked = await crud_approval_request.revoke_request(
            db, request.request_id, current_user.user_id
        )

        if not revoked:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to revoke the request"
            )

        logger.info(f"Approval request {request.request_id} revoked by user {current_user.user_id}")

        return RevokeApprovalResponse(
            id=revoked.id,
            status=revoked.status,
            revoked_at=revoked.revoked_at,
            message="Request has been successfully revoked."
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error revoking approval request")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to revoke approval request"
        )