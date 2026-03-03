from pydantic import BaseModel, Field
from typing import Any, Dict, List
from datetime import datetime
import uuid

# Course recommendation schemas
class RecommendedCourseBase(BaseModel):
    """Base schema for Recommended Course"""
    role_mapping_id: uuid.UUID = Field(..., description="ID of the associated role mapping")
    # actual_courses: List[Dict[str, Any]] = Field(default=[], description="All courses found in search")
    filtered_courses: List[Dict[str, Any]] = Field(default=[], description="Filtered course recommendations")

class RecommendedCourseResponse(RecommendedCourseBase):
    """Schema for Recommended Course response"""
    id: uuid.UUID = Field(..., description="Unique identifier")
    user_id: uuid.UUID = Field(..., description="User ID")
    status: str = Field(..., description="Status")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    
    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            uuid.UUID: lambda v: str(v)
        }
class RecommendCourseCreate(BaseModel):
    """ Course Recommendation Generate"""
    role_mapping_id: uuid.UUID = Field(..., description="ID of the associated role mapping")

