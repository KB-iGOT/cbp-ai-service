from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
import uuid

# Role Mapping Schema
class Competency(BaseModel):
    """Schema for competency"""
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
    designation_name: Optional[str] = Field(None, max_length=255, description="Name of the designation")
    wing_division_section: Optional[str] = Field(None, max_length=255, description="Wing/Division/Section name")
    role_responsibilities: Optional[List[str]] = Field(None, description="List of role responsibilities")
    activities: Optional[List[str]] = Field(None, description="List of activities")
    competencies: Optional[List[Competency]] = Field(None, description="List of competencies")
    sort_order: Optional[int] = Field(None, description="Sort order for hierarchical arrangement")

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
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    
    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            uuid.UUID: lambda v: str(v)
        }

class RoleMappingBackgroundResponse(BaseModel):
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
