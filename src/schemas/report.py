from typing import Any, Dict, List
from ..schemas.role_mapping import RoleMappingResponse


class CompetencyGrouper:
    """Shared utility for grouping competencies by type"""
    
    COMPETENCY_TYPES = {
        'behavioral': ['behaviour', 'behavior', 'behavioral'],
        'functional': ['functional'],
        'domain': ['domain']
    }
    
    @classmethod
    def group_competencies(cls, competencies: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """
        Group competencies into behavioral, functional, and domain categories.

        Args:
            competencies: List of competency dictionaries

        Returns:
            Dictionary with 'behavioral', 'functional', and 'domain' keys, each holding
            a list of {'label', 'proficiency_level', 'delivery_mode'} dictionaries.
            Templates render `label` as before; the two extra keys are None for course
            competencies (iGOT course data carries no proficiency level or delivery mode)
            and `proficiency_level` is None for Domain, which has no KCM level.
        """
        grouped = {
            'behavioral': [],
            'functional': [],
            'domain': []
        }

        if not competencies:
            return grouped

        for comp in competencies:
            comp_type = cls._determine_competency_type(comp)

            if comp_type:
                grouped[comp_type].append(cls._format_competency(comp))

        return grouped

    @classmethod
    def _format_competency(cls, comp: Dict[str, Any]) -> Dict[str, Any]:
        """Build the template-facing competency: display label plus level and delivery mode."""
        return {
            'label': cls._format_competency_label(comp),
            'proficiency_level': (comp.get('proficiency_level') or '').strip() or None,
            'delivery_mode': (comp.get('delivery_mode') or '').strip() or None,
        }

    @classmethod
    def _format_competency_label(cls, comp: Dict[str, Any]) -> str:
        """Format competency label from theme and sub-theme"""
        # Handle both naming conventions
        theme = (comp.get('competencyThemeName') or comp.get('theme') or '').strip()
        sub_theme = (comp.get('competencySubThemeName') or comp.get('sub_theme') or '').strip()
        return f"{theme} - {sub_theme}"
    
    @classmethod
    def _determine_competency_type(cls, comp: Dict[str, Any]) -> str:
        """Determine competency type (behavioral, functional, or domain)"""
        # Handle both naming conventions
        area = (comp.get('competencyAreaName') or comp.get('type', '')).lower()
        
        for comp_type, keywords in cls.COMPETENCY_TYPES.items():
            if any(keyword in area for keyword in keywords):
                return comp_type
        
        return None


class CourseCardData:
    """Formatted course data for template rendering"""
    
    def __init__(self, course: dict):
        self.title = course.get("course") or course.get("name")
        self.relevancy = course.get("relevancy", 0)
        self.is_public = course.get("is_public", False)
        self.provider = self._extract_provider(course)
        
        # Group competencies
        competencies = course.get("competencies") or course.get("competencies_v6") or []
        grouped = CompetencyGrouper.group_competencies(competencies)
        
        self.functional = grouped['functional']
        self.domain = grouped['domain']
        self.behavioral = grouped['behavioral']
    
    def _extract_provider(self, course: dict) -> str:
        """Extract provider from course data"""
        org = course.get("organisation")
        if org and isinstance(org, list) and len(org) > 0:
            return org[0]
        return course.get("platform", '')

    def to_dict(self) -> Dict[str, Any]:
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
        
        # Group competencies using shared utility
        # Convert Pydantic models to dictionaries
        competencies_dict = [comp.model_dump() if hasattr(comp, 'model_dump') else comp for comp in cbp_record.competencies]
        grouped = CompetencyGrouper.group_competencies(competencies_dict)
        
        self.behavioral_competencies = grouped['behavioral']
        self.functional_competencies = grouped['functional']
        self.domain_competencies = grouped['domain']
        
        # Process CBP plans if available
        self.cbp_plans = []
        if cbp_record.cbp_plans and len(cbp_record.cbp_plans) > 0:
            self.cbp_plans = [
                CourseCardData(course).to_dict() 
                for course in cbp_record.cbp_plans[0].selected_courses
            ]
    
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