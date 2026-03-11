from pydantic import BaseModel, Field, field_validator
from typing import List, Optional
from datetime import datetime, date
import re
import uuid

from .role_mapping import Competency, RoleMappingWithoutCBP
from ..models.approval_request import RequestStatus


# Request Schemas

class ApprovalRequestCreate(BaseModel):
    """Schema for creating a new approval request"""
    request_name: str = Field(..., min_length=1, max_length=100, description="Name of the approval request")
    mdo_admin_id: str = Field(..., min_length=1, description="ID of the MDO admin/leader")
    role_mapping_ids: List[uuid.UUID] = Field(..., min_length=1, description="List of role mapping IDs to include in the request")

    @field_validator("request_name")
    @classmethod
    def request_name_no_special_chars(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9 _-]+$", v):
            raise ValueError("Request name can only contain letters, numbers, spaces, underscores (_), and hyphens (-)")
        return v


class ApprovalRequestSearch(BaseModel):
    """Schema for searching/filtering approval requests"""
    state_center_id: Optional[str] = Field(None, description="Filter by state/center ID")
    department_id: Optional[str] = Field(None, description="Filter by department ID")
    status: Optional[str] = Field(None, description="Filter by request status (draft, pending, in_review, approved_published, rejected)")
    search: Optional[str] = Field(None, description="Search by request name (partial) or request ID")
    from_date: Optional[date] = Field(None, description="Filter requests created on or after this date (YYYY-MM-DD)")
    to_date: Optional[date] = Field(None, description="Filter requests created on or before this date (YYYY-MM-DD)")
    page: int = Field(1, ge=1, description="Page number")
    page_size: int = Field(10, ge=1, le=100, description="Items per page")


class CbpPlanInput(BaseModel):
    """Schema for a single CBP plan to add to a role mapping item"""
    original_cbp_plan_id: str = Field(..., description="ID of the original CBP plan")
    recommended_course_id: Optional[str] = Field(None, description="ID of the recommended course")
    selected_courses: List[dict] = Field(default=[], description="List of selected courses for the plan")


class AddCbpPlansRequest(BaseModel):
    """Request body to add/replace CBP plans on a role mapping item"""
    role_mapping_item_id: uuid.UUID = Field(..., description="ID of the RequestedRoleMappingItem to update")
    cbp_plans: List[CbpPlanInput] = Field(..., min_length=1, description="List of CBP plans to set on the item")


class AddCbpPlansResponse(BaseModel):
    """Response after successfully updating CBP plans"""
    role_mapping_item_id: uuid.UUID = Field(..., description="ID of the updated role mapping item")
    message: str = Field(..., description="Success message")


# Response Schemas

class CbpPlanSnapshotResponse(BaseModel):
    """Schema for a snapshot of a CBP plan within a role mapping item"""
    original_cbp_plan_id: str = Field(..., description="ID of the original CBP plan")
    recommended_course_id: Optional[str] = Field(None, description="ID of the recommended course")
    selected_courses: List[dict] = Field(default=[], description="List of selected courses for the plan")

class RoleMappingItemResponse(BaseModel):
    """Schema for role mapping item in approval request detail"""
    id: uuid.UUID = Field(..., description="Unique identifier of the snapshot")
    designation_name: str = Field(..., description="Name of the designation")
    wing_division_section: Optional[str] = Field(None, description="Wing/Division/Section name")
    role_responsibilities: List[str] = Field(default=[], description="List of role responsibilities")
    activities: List[str] = Field(default=[], description="List of activities")
    competencies: List[Competency] = Field(default=[], description="List of competencies")
    igot_designation_name: str = Field(..., description="Designation name from iGOT portal")
    igot_designation_id: str = Field(..., description="Designation ID from iGOT portal")
    sort_order: Optional[int] = Field(None, description="Sort order for hierarchical arrangement")
    
    class Config:
        from_attributes = True


class ApprovalRequestSummary(BaseModel):
    """Schema for approval request summary (list view)"""
    request_id: uuid.UUID = Field(..., description="Unique identifier of the approval request")
    request_name: str = Field(..., description="Name of the approval request")
    mdo_admin_name: str = Field(..., description="Name of the MDO admin/leader")
    state_center_name: str = Field(..., description="Name of the state/center")
    department_name: Optional[str] = Field(None, description="Name of the department")
    designation_count: int = Field(..., description="Number of designations in the request")
    request_status: str = Field(..., description="Current status of the request")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    
    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.strftime("%d %b %Y, %I:%M %p") if v else None,
            uuid.UUID: lambda v: str(v)
        }


class ApprovalRequestDetail(ApprovalRequestSummary):
    """Schema for approval request detail (includes role mapping items)"""
    role_mapping_items: List[RoleMappingItemResponse] = Field(default=[], description="List of role mapping snapshots")


class ApprovalRequestCreateResponse(BaseModel):
    """Schema for approval request creation response"""
    request_id: uuid.UUID = Field(..., description="Unique identifier of the created approval request")
    message: str = Field(..., description="Success message")


class ApprovalRequestListResponse(BaseModel):
    """Schema for approval request list response"""
    requests: List[ApprovalRequestSummary] = Field(default=[], description="List of approval requests")
