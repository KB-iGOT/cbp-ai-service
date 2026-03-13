from typing import Optional
import uuid
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.user import User
from ...schemas.approval_request import (
    ApprovalRequestCreate,
    ApprovalRequestCreateResponse,
    ApprovalRequestListResponse,
    ApprovalRequestDetail,
    ApprovalRequestSearch
)
from ...schemas.mdo_admin import MDOAdminListResponse, MDOAdmin
from ...services.approval_request_service import ApprovalRequestService
from ...services.mdo_admin_service import mdo_admin_service
from ...core.database import get_db_session
from ...core.logger import logger
from ...api.dependencies import get_current_active_user


router = APIRouter(prefix="/approval-requests", tags=["Approval Requests"])


@router.post("/create", response_model=ApprovalRequestCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_approval_request(
    request_data: ApprovalRequestCreate,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Create a new approval request with selected role mappings.
    
    Validates:
    - Request name is 1-100 characters
    - At least one role mapping ID is provided
    - All role mapping IDs exist and belong to the user
    - All role mappings are matched (have igot_designation_id)
    """
    try:
        logger.info(f"Creating approval request for user {current_user.user_id}: {request_data.request_name}")
        
        service = ApprovalRequestService(db)
        request_id = await service.create_approval_request(
            user_id=current_user.user_id,
            request_data=request_data
        )
        
        logger.info(f"Approval request created successfully: {request_id}")
        
        return ApprovalRequestCreateResponse(
            request_id=request_id,
            message="Approval request created successfully"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating approval request: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create approval request: {str(e)}"
        )


@router.post("/search", response_model=ApprovalRequestListResponse, status_code=status.HTTP_200_OK)
async def search_approval_requests(
    search_data: ApprovalRequestSearch,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Search and filter approval requests for the authenticated user.

    Optional filters:
    - state_center_id: Filter by state/center
    - department_id: Filter by department
    - status: Filter by request status (draft, pending, in_review, approved_published, rejected)
    - search: Search by request name (partial) or request ID
    - from_date: Filter requests created on or after this date (YYYY-MM-DD)
    - to_date: Filter requests created on or before this date (YYYY-MM-DD)
    - page: Page number (default: 1)
    - page_size: Items per page (default: 10, max: 100)

    Results are sorted by created_at in descending order (newest first).
    """
    try:
        logger.info(f"Searching approval requests for user {current_user.user_id}")

        service = ApprovalRequestService(db)
        filters = {}

        if search_data.state_center_id:
            filters['state_center_id'] = search_data.state_center_id
        if search_data.department_id:
            filters['department_id'] = search_data.department_id
        if search_data.status:
            filters['status'] = search_data.status
        if search_data.search:
            filters['search'] = search_data.search
        if search_data.from_date:
            filters['from_date'] = search_data.from_date
        if search_data.to_date:
            filters['to_date'] = search_data.to_date

        filters['page'] = search_data.page
        filters['page_size'] = search_data.page_size

        requests = await service.get_approval_requests(
            user_id=current_user.user_id,
            filters=filters
        )

        return ApprovalRequestListResponse(requests=requests)

    except Exception as e:
        logger.error(f"Error searching approval requests: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search approval requests: {str(e)}"
        )


@router.get("/mdo-admins", response_model=MDOAdminListResponse, status_code=status.HTTP_200_OK)
async def get_mdo_admins(
    department_id: str = Query(..., description="Department ID to fetch MDO admins for"),
    current_user: User = Depends(get_current_active_user)
):
    """
    Retrieve MDO (Ministry/Department/Organization) admins and leaders from iGOT portal.
    
    This endpoint fetches users with MDO_ADMIN or MDO_LEADER roles for a specific department.
    The data is fetched from the iGOT Karmayogi portal API.
    
    Returns:
    - List of MDO admins with their ID, first name, last name, role type, and department name
    """
    try:
        logger.info(f"Fetching MDO admins for department: {department_id}")
        
        admins_data = await mdo_admin_service.get_mdo_admins(department_id)
        

        # Transform to simplified format
        admins = []
        for admin in admins_data:
            # Get roles directly from API response
            roles = admin.get('roles', [])
            
            role_type = "MDO_LEADER" if "MDO_LEADER" in roles else "MDO_ADMIN" if "MDO_ADMIN" in roles else ""
            
            # Get department name from organisations
            department_name = "Unknown Department"
            organisations = admin.get('organisations', [])
            if organisations:
                department_name = organisations[0].get('orgName', 'Unknown Department')
            
            admins.append(MDOAdmin(
                id=admin.get('id', ''),
                first_name=admin.get('firstName', ''),
                last_name=admin.get('lastName') or '',
                role_type=role_type,
                department_name=department_name
            ))
        
        # Sort admins by first_name, last_name
        admins.sort(key=lambda x: (x.first_name.lower(), x.last_name.lower()))
        
        return MDOAdminListResponse(
            admins=admins,
            count=len(admins)
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching MDO admins: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch MDO admins: {str(e)}"
        )


@router.get("/{request_id}", response_model=ApprovalRequestDetail, status_code=status.HTTP_200_OK)
async def get_approval_request_details(
    request_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Retrieve detailed information about a specific approval request.
    
    Includes:
    - Request metadata (name, status, timestamps, etc.)
    - All role mapping snapshots with full details
    """
    try:
        logger.info(f"Retrieving approval request details: {request_id}")
        
        service = ApprovalRequestService(db)
        request_detail = await service.get_approval_request_details(
            request_id=request_id,
            user_id=current_user.user_id
        )
        
        return request_detail
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving approval request details: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve approval request details: {str(e)}"
        )


@router.delete("/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_approval_request(
    request_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Revoke and permanently delete an approval request.
    
    Only allowed for requests with status PENDING or IN_REVIEW.
    The creator can revoke their own requests to stop the approval workflow.
    
    After revocation:
    - The approval request and all its role mapping snapshots are permanently deleted
    
    Returns 400 Bad Request if:
    - Request is already APPROVED_PUBLISHED or REJECTED
    - Request is in DRAFT status
    """
    try:
        logger.info(f"Revoking (deleting) approval request: {request_id} by user {current_user.user_id}")
        
        service = ApprovalRequestService(db)
        await service.revoke_approval_request(
            request_id=request_id,
            user_id=current_user.user_id
        )
        
        logger.info(f"Approval request revoked and deleted successfully: {request_id}")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error revoking approval request: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to revoke approval request: {str(e)}"
        )


@router.get("/{request_id}/cbp_plans", status_code=status.HTTP_200_OK)
async def get_cbp_plans(
    request_id: uuid.UUID,
    role_mapping_item_id: uuid.UUID = Query(..., description="ID of the RequestedRoleMappingItem to fetch CBP plans for"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    """
    Retrieve CBP plans for a specific role mapping item within an approval request.

    - `role_mapping_item_id`: ID of the RequestedRoleMappingItem (query param)

    Returns the list of CBP plan snapshots stored on that item.
    """
    try:
        logger.info(
            f"Fetching CBP plans for item {role_mapping_item_id} "
            f"in request {request_id} by user {current_user.user_id}"
        )

        service = ApprovalRequestService(db)
        cbp_plans = await service.get_cbp_plans(
            request_id=request_id,
            user_id=current_user.user_id,
            role_mapping_item_id=role_mapping_item_id
        )

        logger.info(f"Returning {len(cbp_plans)} CBP plans for item: {role_mapping_item_id}")

        return {"role_mapping_item_id": str(role_mapping_item_id), "cbp_plans": cbp_plans}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching CBP plans: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch CBP plans: {str(e)}"
        )
