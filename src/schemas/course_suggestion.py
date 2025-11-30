from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
import uuid

class CompetencyBase(BaseModel):
    competencyAreaName: Optional[str] = None
    competencyThemeName: Optional[str] = None
    competencySubThemeName: Optional[str] = None


# iGOT Course suggestion schemas
class CourseSuggestionRequest(BaseModel):
    skip: int = Field(
        default=0,
        ge=0,
        description="Number of items to skip (offset)"
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=5000,
        description="Maximum number of items to return (page size)"
    )
    search_term: Optional[str] = Field(
        default='',
        description="Search term to filter courses by name, keywords, description, organisation, or identifier"
    )

class CourseSuggestionRespose(BaseModel):
    identifier: Optional[str] = None
    name: Optional[str] = None
    keywords: List[str] = []
    description: Optional[str] = None
    competencies_v6: List[CompetencyBase] = []
    language: List[str] = []
    organisation: List[str] = []
    duration: Optional[str] = None

    class Config:
        from_attributes = True

class CourseSuggestionSave(BaseModel):
    role_mapping_id: uuid.UUID = Field(..., description="Role mapping ID")
    course_identifiers: List[str] = Field(..., description="List of selected course identifiers/IDs from recommendations")

class CourseSuggestionSaveResponse(CourseSuggestionSave):
    id: uuid.UUID
    user_id: uuid.UUID = Field(..., description="User ID")    
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")

    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            uuid.UUID: lambda v: str(v)
        }
