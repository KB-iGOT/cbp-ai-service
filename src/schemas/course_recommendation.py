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

class CourseCardData:
    def __init__(self, course: dict):
        self.title = course.get("course") or course.get("name")
        self.relevancy = course.get("relevancy", 0)
        self.is_public = course.get("is_public", False)

        # Provider
        self.provider = (
            course.get("organisation", [""])[0]
            if course.get("organisation") else course.get("platform", '')
        )

        # Competency buckets
        self.functional = []
        self.domain = []
        self.behavioral = []
        competencies = course.get("competencies") or course.get("competencies_v6")
        for comp in competencies:
            label = f"{comp['competencyThemeName']} - {comp['competencySubThemeName']}"
            area = comp["competencyAreaName"].lower()

            if "functional" in area:
                self.functional.append(label)
            elif "domain" in area:
                self.domain.append(label)
            elif "behaviour" in area or "behavior" in area:
                self.behavioral.append(label)

    def to_dict(self):
        return {
            "title": self.title,
            "provider": self.provider,
            "relevancy": self.relevancy,
            "is_public": self.is_public,
            "functional": self.functional,
            "domain": self.domain,
            "behavioral": self.behavioral,
        }