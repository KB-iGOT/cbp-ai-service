from pydantic import BaseModel, Field
from typing import  List, Optional
from datetime import datetime
import uuid
from ..schemas.course_suggestion import CompetencyBase

# New Course API schema
class UserAddedCourseBase(BaseModel):
    """Base schema for User Added Course"""
    role_mapping_id: uuid.UUID = Field(..., description="ID of the associated role mapping")
    name: str = Field(..., min_length=1, max_length=500, description="Full name of the course")
    platform: str = Field(..., min_length=1, max_length=100, description="Platform where the course is hosted")
    public_link: str = Field(..., description="Public URL to the specific course")
    relevancy: Optional[int] = Field(None, ge=0, le=100, description="Relevancy score from 0 to 100")
    rationale: Optional[str] = Field(None, description="Brief explanation of why this course is essential")
    language: Optional[str] = Field(None, max_length=10, description="Language of the course (e.g., en, hi)")
    competencies: Optional[List[CompetencyBase]] = Field(None, description="Array of competency objects")

class UserAddedCourseCreate(UserAddedCourseBase):
    """Schema for creating a User Added Course"""
    pass

class UserAddedCourseUpdate(BaseModel):
    """Schema for updating a User Added Course"""
    name: Optional[str] = Field(None, min_length=1, max_length=500, description="Full name of the course")
    platform: Optional[str] = Field(None, min_length=1, max_length=100, description="Platform where the course is hosted")
    public_link: Optional[str] = Field(None, description="Public URL to the specific course")
    relevancy: Optional[int] = Field(None, ge=0, le=100, description="Relevancy score from 0 to 100")
    rationale: Optional[str] = Field(None, description="Brief explanation of why this course is essential")
    language: Optional[str] = Field(None, max_length=10, description="Language of the course")
    competencies: Optional[List[CompetencyBase]] = Field(None, description="Array of competency objects")

class UserAddedCourseResponse(UserAddedCourseBase): 
    """Schema for User Added Course response"""
    id: uuid.UUID = Field(..., description="Unique identifier")
    identifier: uuid.UUID = Field(..., description="Course identifier")
    user_id: uuid.UUID = Field(..., description="User ID who added the course")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    
    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            uuid.UUID: lambda v: str(v)
        }

class BulkDeleteResponse(BaseModel):
    """Schema for bulk delete response"""
    message: str = Field(..., description="Delete operation message")
    deleted_count: int = Field(..., description="Number of courses deleted")
    role_mapping_id: str = Field(..., description="Role mapping ID")

class CourseDeleteResponse(BaseModel):
    """Schema for single course delete response"""
    message: str = Field(..., description="Delete operation message")
    course_id: str = Field(..., description="Deleted course ID")
