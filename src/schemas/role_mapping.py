from enum import Enum
from pydantic import BaseModel, Field, model_validator
from typing import Any, Dict, List, Optional
from datetime import datetime
import uuid


class MatchedDesignationDetail(BaseModel):
    role_mapping_id: str
    igot_designation_name: str
    igot_designation_id: str

class OrgType(str, Enum):
    ministry = "ministry"
    state = "state"

class Competency(BaseModel):
    """Schema for competency"""
    competency_id: Optional[str] = Field(default=None, description="KCM competency id (e.g. BEH-07 / FUN-23); present for Behavioural/Functional, absent for Domain")
    type: str = Field(..., description="Type of competency (Behavioral, Functional, Domain)")
    theme: str = Field(..., description="Theme of the competency")
    sub_theme: str = Field(..., description="Sub-theme of the competency")

class RoleMappingBase(BaseModel):
    """Base schema for Role Mapping"""
    state_center_id: str = Field(..., description="ID of the associated state/center")
    state_center_name: str = Field(..., description="Name of the associated state/center")
    department_id: Optional[str] = Field(None, description="ID of the associated department")
    department_name: Optional[str] = Field(None, description="Name of the associated department")
    sector_name: Optional[str] = Field(None, max_length=255, description="Name of the sector")
    instruction: Optional[str] = Field(None, description="Additional instructions for role mapping generation")

class RoleMappingGenerateRequest(RoleMappingBase):
    """Schema for role mapping generation request"""
    pass

class RoleMappingUpdate(BaseModel):
    """Schema for updating a Role Mapping"""
    sector_name: Optional[str] = Field(None, max_length=255, description="Name of the sector")
    instruction: Optional[str] = Field(None, description="Additional instructions")
    designation_name: Optional[str] = Field(None, max_length=255, description="Name of the designation from iGOT portal")
    igot_designation_id: Optional[str] = Field(None, max_length=255, description="ID of the designation from iGOT portal")
    wing_division_section: Optional[str] = Field(None, max_length=255, description="Wing/Division/Section name")
    role_responsibilities: Optional[List[str]] = Field(None, description="List of role responsibilities")
    activities: Optional[List[str]] = Field(None, description="List of activities")
    competencies: Optional[List[Competency]] = Field(None, description="List of competencies")
    sort_order: Optional[int] = Field(None, description="Sort order for hierarchical arrangement")

class CBPPlan(BaseModel):
    """Schema for CBP plan save response"""
    id: uuid.UUID = Field(..., description="Unique identifier")
    user_id: uuid.UUID = Field(..., description="User ID")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    selected_courses: List[Dict[str, Any]] = Field(..., description="Selected course details")

    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            uuid.UUID: lambda v: str(v)
        }

class DesignationApprovalInfo(BaseModel):
    """Nested schema for designation approval within role mapping response"""
    rolemapping_id: uuid.UUID = Field(..., description="Role mapping ID")
    status: str = Field(..., description="Approval status (pending, approved, rejected)")
    reviewer_comments: Optional[str] = Field(None, description="Reviewer comments")

    class Config:
        from_attributes = True
        json_encoders = {
            uuid.UUID: lambda v: str(v)
        }


class RoleMappingResponse(RoleMappingBase):
    """Schema for Role Mapping response"""
    id: uuid.UUID = Field(..., description="Unique identifier")
    user_id: uuid.UUID = Field(..., description="User ID")
    designation_name: str = Field(..., min_length=1, max_length=255, description="Name of the designation")
    status: str = Field(..., description="Status")
    wing_division_section: str = Field(..., max_length=255, description="Wing/Division/Section name")
    role_responsibilities: List[str] = Field(default=[], description="List of role responsibilities")
    activities: List[str] = Field(default=[], description="List of activities")
    competencies: List[Competency] = Field(default=[], description="List of competencies")
    sort_order: Optional[int] = Field(None, description="Sort order for hierarchical arrangement")
    igot_designation_name: Optional[str] = Field(None, description="Designation name as it exists in the iGOT portal")
    igot_designation_id: Optional[str] = Field(None, description="Designation ID from the iGOT portal")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    
    # Add CBP plans relationship
    cbp_plans: List[CBPPlan] = Field(default=[], description="List of CBP plans associated with this role mapping")
    # Add designation approval relationship
    designation_approval: Optional[DesignationApprovalInfo] = Field(None, description="Designation approval status for this role mapping")

    @model_validator(mode='before')
    @classmethod
    def extract_designation_approval(cls, data):
        """Extract the approved designation approval from the relationship list."""
        approvals = getattr(data, 'designation_approvals', None)
        if approvals and len(approvals) > 0:
            data.__dict__['designation_approval'] = approvals[0]
        return data

    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            uuid.UUID: lambda v: str(v)
        }

class RoleMappingWithoutCBP(RoleMappingBase):
    """Schema for Role Mapping response"""
    id: uuid.UUID = Field(..., description="Unique identifier")
    user_id: uuid.UUID = Field(..., description="User ID")
    designation_name: str = Field(..., min_length=1, max_length=255, description="Name of the designation")
    status: str = Field(..., description="Status")
    wing_division_section: str = Field(..., max_length=255, description="Wing/Division/Section name")
    role_responsibilities: List[str] = Field(default=[], description="List of role responsibilities")
    activities: List[str] = Field(default=[], description="List of activities")
    competencies: List[Competency] = Field(default=[], description="List of competencies")
    sort_order: Optional[int] = Field(None, description="Sort order for hierarchical arrangement")
    igot_designation_name: Optional[str] = Field(None, description="Designation name as it exists in the iGOT portal")
    igot_designation_id: Optional[str] = Field(None, description="Designation ID from the iGOT portal")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            uuid.UUID: lambda v: str(v)
        }

class RoleMappingBackgroundResponse(BaseModel):
    is_existing: bool = Field(
        ..., 
        description="Indicates whether the role mapping already existed (true) or was newly generated (false)."
    )
    message: str = Field(..., description="A success or error message detailing the result of the operation.")
    status: str = Field(..., description="The status of the operation (e.g., 'success', 'failed', 'pending').")
    role_mappings: List[RoleMappingResponse] = Field(default_factory=list, description="A list of the role mapping objects.")


# Schemas for adding designation
class AddDesignationToRoleMappingRequest(BaseModel):
    """Schema for adding new designation to existing role mapping"""
    state_center_id: str = Field(..., description="ID of the associated state/center")
    state_center_name: str = Field(..., description="Name of the associated state/center")
    department_id: Optional[str] = Field(None, description="ID of the associated department")
    department_name: Optional[str] = Field(None, description="Name of the associated department")
    designation_name: str = Field(..., min_length=1, max_length=255, description="New designation names")
    instruction: Optional[str] = Field(None, description="Additional instructions for role mapping generation")


class DesignationOrderItem(BaseModel):
    """Schema for a single designation order item"""
    id: uuid.UUID = Field(..., description="Role mapping ID")
    sort_order: int = Field(..., ge=1, description="New sort order position (1-based)")


class ReorderDesignationsRequest(BaseModel):
    """Schema for reordering designations via drag and drop"""
    state_center_id: str = Field(..., description="ID of the associated state/center")
    department_id: Optional[str] = Field(None, description="ID of the associated department")
    designations: List[DesignationOrderItem] = Field(..., min_length=1, description="List of designations with their new sort orders")


class matchedDesignationsRequest(BaseModel):
    """Schema for validating role mapping designations against the iGOT portal"""
    state_center_id: str = Field(..., description="ID of the state/center whose role mappings to matched")
    department_id: Optional[str] = Field(None, description="Optional department ID to narrow the scope")


class RoleMappingReorderListItem(BaseModel):
    """Lightweight schema for the reorder list view"""
    id: uuid.UUID = Field(..., description="Role mapping ID")
    designation_name: str = Field(..., description="Name of the designation")
    wing_division_section: str = Field(..., description="Wing/Division/Section name")
    sort_order: Optional[int] = Field(None, description="Sort order for hierarchical arrangement")

    class Config:
        from_attributes = True


class MatchStatus(str, Enum):
    matched = "matched"
    unmatched = "unmatched"


class RoleMappingSearchFilters(BaseModel):
    """Nested filters for role mapping search"""
    state_center_id: str = Field(None, description="ID of the associated state/center")
    department_id: Optional[str] = Field(None, description="ID of the associated department")
    match_status: Optional[MatchStatus] = Field(
        None,
        description="Filter data by iGOT match status: 'matched' or 'unmatched'. "
                    "Omit to return both. Does not affect total/total_matched/total_unmatched counts."
    )

    @model_validator(mode='before')
    @classmethod
    def blank_match_status_to_none(cls, data):
        """Treat an empty string match_status (e.g. '') as omitted."""
        if isinstance(data, dict) and data.get('match_status') == '':
            data['match_status'] = None
        return data


class RoleMappingSearchRequest(BaseModel):
    """Schema for role mapping search request"""
    query: Optional[str] = Field(None, description="Search by designation name")
    limit: int = Field(20, ge=1, le=100, description="Number of records to return")
    offset: int = Field(0, ge=0, description="Number of records to skip")
    load_cbp_plans: bool = Field(False, description="Include CBP plans in the response")
    filters: Optional[RoleMappingSearchFilters] = Field(None, description="Additional filters")
    sort_by: Optional[Dict[str, str]] = Field(
        None,
        description="Sort field and direction, e.g. {\"createdOn\": \"desc\"}. "
                    "Defaults to sort_order ascending. Supported fields: "
                    "createdOn, updatedOn, designationName, sortOrder."
    )


class RoleMappingSearchResponse(BaseModel):
    """Response schema for role mapping search"""
    total: int = Field(..., description="Total number of role mappings matching the query/filters")
    total_matched: int = Field(..., description="Total designations matched with an iGOT designation (igot_designation_id populated)")
    total_unmatched: int = Field(..., description="Total designations not yet matched with an iGOT designation")
    data: List[RoleMappingResponse] = Field(default_factory=list, description="Page of matching role mappings")


class DesignationmatchedResult(BaseModel):
    """Response schema for designation matched result"""
    total_designations: int = Field(..., description="Total unique designations from role mappings")
    matched_count: int = Field(..., description="Number of designations found in the iGOT portal")
    already_matched: bool = Field(False, description="True when all designations were already matched in the DB")
    matched_details: List[MatchedDesignationDetail] = Field(default_factory=list, description="List of matched designation details")
