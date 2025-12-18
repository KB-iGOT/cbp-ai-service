from pydantic import BaseModel, Field
from typing import Any, Dict, List
from datetime import datetime
import uuid

from ..schemas.role_mapping import RoleMappingResponse


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

class DesignationData:
    """Formatted designation data for template rendering"""
    def __init__(self, cbp_record: RoleMappingResponse):
        self.designation = cbp_record.designation_name
        self.wing = cbp_record.wing_division_section
        self.roles_responsibilities = cbp_record.role_responsibilities
        self.activities = cbp_record.activities
        
        # Group competencies by type
        self.behavioral_competencies = []
        self.functional_competencies = []
        self.domain_competencies = []

        for comp in cbp_record.competencies:
            comp_str = f"{comp['theme']} - {comp['sub_theme']}"
            comp_type = comp['type'].lower()
           
            if "behavioral" in comp_type:
                self.behavioral_competencies.append(comp_str)
            elif "functional" in comp_type:
                self.functional_competencies.append(comp_str)
            elif "domain" in comp_type:
                self.domain_competencies.append(comp_str)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "designation": self.designation,
            "wing": self.wing,
            "rolesResponsibilities": self.roles_responsibilities,
            "activities": self.activities,
            "behavioralCompetencies": self.behavioral_competencies,
            "functionalCompetencies": self.functional_competencies,
            "domainCompetencies": self.domain_competencies
        }
