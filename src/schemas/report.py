from typing import Any, Dict
from ..schemas.role_mapping import RoleMappingResponse


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
    
class DesignationData:
    """Formatted designation data for template rendering"""
    def __init__(self, cbp_record: RoleMappingResponse):
        self.designation = cbp_record.designation_name
        self.wing = cbp_record.wing_division_section
        self.roles_responsibilities = cbp_record.role_responsibilities
        self.activities = cbp_record.activities
        self.cbp_plans = []
        
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


        if cbp_record.cbp_plans:
            self.cbp_plans = [CourseCardData(course).to_dict() for course in cbp_record.cbp_plans[0].selected_courses]

    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "designation": self.designation,
            "wing": self.wing,
            "rolesResponsibilities": self.roles_responsibilities,
            "activities": self.activities,
            "behavioralCompetencies": self.behavioral_competencies,
            "functionalCompetencies": self.functional_competencies,
            "domainCompetencies": self.domain_competencies,
            "cbp_plans": self.cbp_plans
        }