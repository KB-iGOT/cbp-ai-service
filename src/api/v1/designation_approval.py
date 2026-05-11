from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_active_user
from ...core.database import get_db_session
from ...core.logger import logger
from ...crud.designation_approval import crud_designation_approval
from ...crud.role_mapping import crud_role_mapping
from ...models.user import User
from ...schemas.designation_approval import (
    DesignationApprovalCreate,
    DesignationApprovalListResponse,
    DesignationApprovalResponse,
)

router = APIRouter(prefix="/designation-approval", tags=["Designation Approval"])


@router.post(
    "/create",
    response_model=DesignationApprovalResponse,
    status_code=http_status.HTTP_201_CREATED,
)
async def create_designation_approval(
    request_data: DesignationApprovalCreate,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
):
    """
    Send a designation name for approval.
    Duplicate check: same rolemapping_id + user_id + designation_name + wing_division_section → 409.
    """
    try:
        # Verify rolemapping exists and belongs to this user
        role_mapping = await crud_role_mapping.get_by_id_and_user(
            db=db,
            role_mapping_id=request_data.rolemapping_id,
            user_id=current_user.user_id,
        )
        if not role_mapping:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Role mapping not found.",
            )

        # Only unmatched designations (no igot_designation_id) are allowed
        if role_mapping.igot_designation_id is not None and role_mapping.igot_designation_id != "":
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="This designation is already matched to an iGOT designation. Only unmatched designations can be submitted for approval.",
            )

        existing = await crud_designation_approval.check_duplicate(
            db=db,
            rolemapping_id=request_data.rolemapping_id,
            user_id=current_user.user_id,
            designation_name=request_data.designation_name,
            wing_division_section=request_data.wing_division_section,
        )

        if existing:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="Designation approval request already exists for this role mapping with the same details.",
            )

        record = await crud_designation_approval.create(
            db=db,
            rolemapping_id=request_data.rolemapping_id,
            user_id=current_user.user_id,
            designation_name=request_data.designation_name,
            wing_division_section=request_data.wing_division_section,
        )

        logger.info(f"Designation approval created: {record.id} for user {current_user.user_id}")
        return record

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating designation approval: {str(e)}")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create designation approval: {str(e)}",
        )


@router.get(
    "/search",
    response_model=DesignationApprovalListResponse,
    status_code=http_status.HTTP_200_OK,
)
async def search_designation_approvals(
    designation_name: Optional[str] = Query(None, description="Filter by designation name (partial match)"),
    approval_status: Optional[str] = Query(None, alias="status", description="Filter by status: pending, approved, rejected"),
    from_date: Optional[date] = Query(None, description="Filter from date (YYYY-MM-DD)"),
    to_date: Optional[date] = Query(None, description="Filter to date (YYYY-MM-DD)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
):
    """
    Search/filter designation approval requests.
    Filters: designation_name, status, date range. All optional.
    Always returns paginated table data.
    """
    try:
        records, total = await crud_designation_approval.search(
            db=db,
            user_id=current_user.user_id,
            designation_name=designation_name,
            status=approval_status,
            from_date=from_date,
            to_date=to_date,
            page=page,
            page_size=page_size,
        )

        return DesignationApprovalListResponse(
            total=total,
            page=page,
            page_size=page_size,
            requests=[DesignationApprovalResponse.model_validate(r) for r in records],
        )

    except Exception as e:
        logger.error(f"Error searching designation approvals: {str(e)}")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search designation approvals: {str(e)}",
        )
