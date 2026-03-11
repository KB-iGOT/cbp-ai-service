"""
Schemas for MDO (Ministry/Department/Organization) admin and leader data
"""
from pydantic import BaseModel, Field
from typing import List, Optional


class MDOAdmin(BaseModel):
    """Schema for MDO admin/leader user"""
    id: str = Field(..., description="User ID")
    first_name: str = Field(..., description="First name of the user")
    last_name: str = Field(..., description="Last name of the user")
    role_type: str = Field(..., description="Role type: MDO_ADMIN or MDO_LEADER")
    department_name: str = Field(..., description="Department/Organization name")


class MDOAdminListResponse(BaseModel):
    """Schema for MDO admin list response"""
    admins: List[MDOAdmin] = Field(default=[], description="List of MDO admins and leaders")
    count: int = Field(..., description="Total count of admins")
