from pydantic import BaseModel, Field
from typing import Optional

class DepartmentResponse(BaseModel):
    """Schema for Department response from iGOT Karmayogi API"""
    identifier: str = Field(..., description="Unique department identifier")
    orgName: str = Field(..., description="Department/Organization name")
    description: Optional[str] = Field(None, description="Department description")
    parentOrgName: Optional[str] = Field(None, description="Parent organization name")
    ministryOrStateId: Optional[str] = Field(None, description="Ministry or State ID")
    ministryOrStateType: Optional[str] = Field(None, description="Type: ministry or state")
    ministryOrStateName: Optional[str] = Field(None, description="Ministry or State name")
    sbOrgSubType: Optional[str] = Field(None, description="Organization sub-type")
    
    class Config:
        from_attributes = True
