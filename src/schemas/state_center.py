from pydantic import BaseModel, Field
from typing import List, Optional
from enum import Enum

class OrgTypeEnum(str, Enum):
    ministry = "ministry"
    state = "state"    
class StateCenterResponse(BaseModel):
    """Schema for State/Center response from iGOT Karmayogi API"""
    identifier: str = Field(..., description="Unique organization identifier")
    orgName: str = Field(..., description="Organization name")
    sbOrgType: str = Field(..., description="Organization type (ministry/state)")
    sbOrgSubType: Optional[str] = Field(None, description="Organization sub-type")
    parentOrgName: Optional[str] = Field(None, description="Parent organization name")
    orgHierarchyFrameworkId: Optional[str] = Field(None, description="Organization hierarchy framework ID")
    orgHierarchyFrameworkStatus: Optional[str] = Field(None, description="Hierarchy framework status")
    isRootOrg: Optional[bool] = Field(None, description="Is root organization")
    
    class Config:
        from_attributes = True