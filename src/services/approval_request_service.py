import uuid
import re
from typing import List, Optional, Dict, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException, status

from ..models.role_mapping import RoleMapping
from ..models.approval_request import RequestStatus
from ..crud.approval_request import crud_approval_request
from ..schemas.approval_request import (
    ApprovalRequestCreate,
    ApprovalRequestSummary,
    ApprovalRequestDetail
)
from ..services.mdo_admin_service import mdo_admin_service
from ..models.course_recommendation import RecommendationStatus
from ..schemas.role_mapping import RoleMappingWithoutCBP


class ApprovalRequestService:
    """Service layer for approval request business logic"""
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    async def create_approval_request(
        self,
        user_id: uuid.UUID,
        request_data: ApprovalRequestCreate
    ) -> uuid.UUID:
        """
        Create a new approval request with role mapping snapshots.
        
        Args:
            user_id: ID of the user creating the request
            request_data: Approval request creation data
            
        Returns:
            The request_id of the created approval request
            
        Raises:
            HTTPException: If validation fails
        """
        # Validate that all role mappings exist and belong to the user
        role_mappings = await crud_approval_request.get_role_mappings_by_ids(
            self.db,
            request_data.role_mapping_ids,
            user_id
        )
        
        if len(role_mappings) != len(request_data.role_mapping_ids):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="One or more role mappings not found"
            )
        
        # Validate that all role mappings are matched (have igot_designation_id)
        unmatched = [rm for rm in role_mappings if not rm.igot_designation_id]
        if unmatched:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="All selected role mappings must be matched (have igot_designation_id)"
            )
        
        # Validate that all role mappings have completed course recommendations
        missing_course_recommendations = []
        for rm in role_mappings:
            has_completed_recommendation = False
            if rm.recommended_courses:
                has_completed_recommendation = any(
                    rc.status == RecommendationStatus.COMPLETED.value 
                    for rc in rm.recommended_courses
                )
            
            if not has_completed_recommendation:
                missing_course_recommendations.append(rm.designation_name or str(rm.id))
        
        if missing_course_recommendations:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"The following role mappings do not have completed course recommendations: {', '.join(missing_course_recommendations)}"
            )
        
        # Derive state_center_id, department_id, and names from first role mapping
        first_rm = role_mappings[0]
        state_center_id = first_rm.state_center_id
        state_center_name = first_rm.state_center_name or ""
        department_id = first_rm.department_id
        department_name = first_rm.department_name or ""
        
        # Fetch MDO admin details from iGOT portal
        try:
            admins_data = await mdo_admin_service.get_mdo_admins(department_id)
            
            # Find the specific admin by ID
            mdo_admin = next((admin for admin in admins_data if admin['id'] == request_data.mdo_admin_id), None)
            
            if not mdo_admin:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"MDO admin with ID {request_data.mdo_admin_id} not found"
                )
            
            # Construct full name from firstName and lastName (handle None values)
            first_name = mdo_admin.get('firstName', '') or ''
            last_name = mdo_admin.get('lastName', '') or ''
            mdo_admin_name = f"{first_name} {last_name}".strip()
            
            # If both are empty, use a default
            if not mdo_admin_name:
                mdo_admin_name = "Unknown Admin"
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch MDO admin details: {str(e)}"
            )
        
        # Create snapshots from role mappings
        snapshots = []
        for rm in role_mappings:
            snapshots.append({
                'id': rm.id,
                'designation_name': rm.designation_name,
                'wing_division_section': rm.wing_division_section,
                'role_responsibilities': rm.role_responsibilities or [],
                'activities': rm.activities or [],
                'competencies': rm.competencies or [],
                'igot_designation_name': rm.igot_designation_name,
                'igot_designation_id': rm.igot_designation_id,
                'sort_order': rm.sort_order,
                'cbp_plans': [
                    {
                        'original_cbp_plan_id': str(plan.id) if plan.id else "",
                        'recommended_course_id': str(plan.recommended_course_id) if plan.recommended_course_id else None,
                        'selected_courses': plan.selected_courses or []
                    } for plan in getattr(rm, 'cbp_plans', [])
                ]
            })
        
        # Create the approval request
        approval_request = await crud_approval_request.create(
            self.db,
            user_id=user_id,
            request_name=request_data.request_name,
            mdo_admin_id=request_data.mdo_admin_id,
            mdo_admin_name=mdo_admin_name,
            state_center_id=state_center_id,
            state_center_name=state_center_name,
            department_id=department_id,
            department_name=department_name,
            role_mapping_snapshots=snapshots
        )
        
        return approval_request.request_id
    
    async def get_approval_requests(
        self,
        user_id: uuid.UUID,
        filters: Optional[Dict] = None
    ) -> List[ApprovalRequestSummary]:
        """
        Retrieve all approval requests for a user with optional filters.
        
        Args:
            user_id: ID of the user
            filters: Optional dict with state_center_id, department_id, status, search, time_filter, page, page_size
            
        Returns:
            List of ApprovalRequestSummary objects
        """
        filters = filters or {}
        
        requests = await crud_approval_request.get_by_user(
            self.db,
            user_id=user_id,
            state_center_id=filters.get('state_center_id'),
            department_id=filters.get('department_id'),
            status=filters.get('status'),
            search=filters.get('search'),
            from_date=filters.get('from_date'),
            to_date=filters.get('to_date'),
            page=filters.get('page', 1),
            page_size=filters.get('page_size', 10)
        )
        
        return [ApprovalRequestSummary.model_validate(req) for req in requests]
    
    async def get_approval_request_details(
        self,
        request_id: uuid.UUID,
        user_id: uuid.UUID
    ) -> ApprovalRequestDetail:
        """
        Retrieve detailed information about a specific approval request.
        
        Args:
            request_id: ID of the approval request
            user_id: ID of the user (for ownership verification)
            
        Returns:
            ApprovalRequestDetail object
            
        Raises:
            HTTPException: If request not found or unauthorized
        """
        request = await crud_approval_request.get_by_id(self.db, request_id)
        
        if not request:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request with ID {request_id} not found"
            )
        
        if request.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this approval request"
            )
        
        return ApprovalRequestDetail.model_validate(request)
    
    def validate_status_transition(
        self,
        current_status: RequestStatus,
        new_status: RequestStatus
    ) -> bool:
        """
        Validate if a status transition is allowed according to state machine rules.
        
        Valid transitions:
        - draft → pending (resubmit after revoke)
        - pending → in_review
        - pending → draft (revoke)
        - in_review → approved_published
        - in_review → rejected
        - in_review → draft (revoke)
        
        Args:
            current_status: Current status
            new_status: Desired new status
            
        Returns:
            True if transition is valid, False otherwise
        """
        valid_transitions = {
            RequestStatus.DRAFT: [RequestStatus.PENDING],
            RequestStatus.PENDING: [RequestStatus.IN_REVIEW, RequestStatus.DRAFT],
            RequestStatus.IN_REVIEW: [RequestStatus.APPROVED_PUBLISHED, RequestStatus.REJECTED, RequestStatus.DRAFT],
            RequestStatus.APPROVED_PUBLISHED: [],  # Terminal state
            RequestStatus.REJECTED: []   # Terminal state
        }
        
        return new_status in valid_transitions.get(current_status, [])
    
    async def revoke_approval_request(
        self,
        request_id: uuid.UUID,
        user_id: uuid.UUID
    ) -> None:
        """
        Revoke and permanently delete an approval request.
        Only allowed for PENDING or IN_REVIEW status.
        
        Args:
            request_id: ID of the approval request
            user_id: ID of the user (for ownership verification)
            
        Raises:
            HTTPException: If request not found, unauthorized, or invalid status
        """
        # Get current request
        request = await crud_approval_request.get_by_id(self.db, request_id)
        
        if not request:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request with ID {request_id} not found"
            )
        
        # Verify ownership
        if request.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to revoke this approval request"
            )
        
        # Check if revoke is allowed (only for PENDING or IN_REVIEW)
        current_status = RequestStatus(request.request_status)
        if current_status not in [RequestStatus.PENDING, RequestStatus.IN_REVIEW]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot revoke request with status {current_status.value}. Only PENDING or IN_REVIEW requests can be revoked."
            )
        
        # Permanently delete the approval request
        await crud_approval_request.delete(self.db, request_id)

    async def add_cbp_plans(
        self,
        request_id: uuid.UUID,
        user_id: uuid.UUID,
        role_mapping_item_id: uuid.UUID,
        cbp_plans: list
    ) -> uuid.UUID:
        """
        Add/replace CBP plans on a RequestedRoleMappingItem.

        Args:
            request_id: ID of the parent approval request
            user_id: ID of the authenticated user (ownership check)
            role_mapping_item_id: ID of the RequestedRoleMappingItem to update
            cbp_plans: List of CBP plan dicts to set

        Returns:
            The role_mapping_item_id that was updated

        Raises:
            HTTPException: If request/item not found, unauthorized
        """
        # Verify the approval request exists and belongs to user
        request = await crud_approval_request.get_by_id(self.db, request_id)

        if not request:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request with ID {request_id} not found"
            )

        if request.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify this approval request"
            )

        # Serialize plans to plain dicts for JSONB storage
        plans_data = [plan if isinstance(plan, dict) else plan.model_dump() for plan in cbp_plans]

        # Update the cbp_plans on the item (also validates item belongs to request)
        updated_item = await crud_approval_request.update_cbp_plans(
            self.db,
            request_id=request_id,
            item_id=role_mapping_item_id,
            cbp_plans=plans_data
        )

        if not updated_item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Role mapping item with ID {role_mapping_item_id} not found in request {request_id}"
            )

        return updated_item.id

    async def get_cbp_plans(
        self,
        request_id: uuid.UUID,
        user_id: uuid.UUID,
        role_mapping_item_id: uuid.UUID
    ) -> list:
        """
        Retrieve CBP plans for a specific role mapping item.

        Args:
            request_id: ID of the parent approval request
            user_id: ID of the authenticated user (ownership check)
            role_mapping_item_id: ID of the RequestedRoleMappingItem

        Returns:
            List of CBP plan dicts stored on the item

        Raises:
            HTTPException: If request/item not found or unauthorized
        """
        request = await crud_approval_request.get_by_id(self.db, request_id)

        if not request:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request with ID {request_id} not found"
            )

        if request.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this approval request"
            )

        item = await crud_approval_request.get_item_by_id(
            self.db,
            request_id=request_id,
            item_id=role_mapping_item_id
        )

        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Role mapping item with ID {role_mapping_item_id} not found in request {request_id}"
            )

        return item.cbp_plans or []
        
