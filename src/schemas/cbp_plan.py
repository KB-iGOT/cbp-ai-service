from pydantic import BaseModel, Field
from typing import Any, Dict, List
from datetime import datetime
import uuid

# Schemas for CBP Plan
class CBPPlanSaveRequest(BaseModel):
    """Schema for saving CBP plan with selected courses"""
    role_mapping_id: uuid.UUID = Field(..., description="Role mapping ID")
    recommended_course_id: uuid.UUID = Field(..., description="Role mapping ID")
    course_identifiers: List[str] = Field(..., description="List of selected course identifiers/IDs from recommendations")

class CBPPlanUpdateRequest(BaseModel):
    """Schema for saving CBP plan with selected courses"""
    course_identifiers: List[str] = Field(..., description="List of selected course identifiers/IDs from recommendations")


class CBPPlanSaveResponse(BaseModel):
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
