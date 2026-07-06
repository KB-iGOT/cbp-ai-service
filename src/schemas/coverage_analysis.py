from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class CoverageCompetencyItem(BaseModel):
    """One required competency from the designation's competency framework."""
    type: str = Field(..., description="Behavioral | Functional | Domain")
    theme: str = Field(..., description="Theme of the competency")
    sub_theme: str = Field(..., description="Sub-theme of the competency")


class CoverageCourseItem(BaseModel):
    """One recommended course to use as evidence — caller supplies these directly
    from RecommendedCourse.filtered_courses (+ course metadata), no DB lookup here.

    competency_type is the category (Domain | Behavioral | Functional) this course was
    ALREADY recommended under — assigned by the LLM at course-selection time (see
    get_filtered_courses_by_llm's response_schema in api/v1/course_recommendation.py) and
    persisted as-is on every entry in filtered_courses. This is the authoritative signal
    used to decide which competency section a course is evidence for.

    Course metadata used for comparison: title, description, keywords — plus, for Behavioral/
    Functional courses only, the course's own competency theme/sub-theme tags (competencies,
    below) enriched with their KCM theme/sub-theme descriptions. Domain courses are compared
    on title/description/keywords only — see DOMAIN_COMPETENCY_MATCHING_GUIDANCE for why
    competency tags aren't a meaningful signal for Domain."""
    identifier: str = Field(..., description="Course identifier")
    name: str = Field(..., description="Course title")
    competency_type: str = Field(
        ..., description="Domain | Behavioral | Functional — the category this course was recommended under"
    )
    description: Optional[str] = Field(None, description="Course description")
    keywords: Optional[List[str]] = Field(None, description="Course keywords")
    competencies: Optional[List[Dict[str, Any]]] = Field(
        None,
        description=(
            "Course's own competencies_v6-shaped tags (competencyAreaName/competencyThemeName/"
            "competencySubThemeName). Only used for Behavioral/Functional courses — ignored for Domain."
        ),
    )


class CoverageAnalysisRequest(BaseModel):
    """Request body — everything needed is supplied directly, no DB access in this endpoint."""
    designation_name: str = Field(..., description="Designation name")
    department_name: Optional[str] = Field(None, description="Department name")
    organisation_name: Optional[str] = Field(None, description="Ministry/State/Organisation name")
    competencies: List[CoverageCompetencyItem] = Field(
        ..., min_length=1, description="Full competency framework for this designation"
    )
    courses: List[CoverageCourseItem] = Field(
        ..., min_length=1, description="The recommended courses to use as evidence"
    )


class CompetencyCoverageResult(BaseModel):
    competency: str
    theme: str
    sub_theme: str
    coverage: str = Field(..., description="Fully Covered | Partially Covered | Not Covered")
    supporting_courses: List[str] = Field(default_factory=list)
    reason: str


class CourseScoreResult(BaseModel):
    """Per-course, per-competency-type relevance score — complements CompetencyCoverageResult
    (which is per-competency, aggregated across courses) with the inverse view: per-course,
    aggregated across all competencies of one type. coverage_bracket is computed in code from
    coverage_score via fixed thresholds (see FULLY_COVERED_SCORE_THRESHOLD /
    PARTIALLY_COVERED_SCORE_THRESHOLD in api/v1/coverage_analysis.py), not asked from the LLM
    directly, so the label is always consistent with the numeric score."""
    course_identifier: str
    course_name: str
    coverage_score: int = Field(..., ge=0, le=100)
    coverage_bracket: str = Field(..., description="Fully Covered | Partially Covered | Not Covered")
    reason: str


class CoverageAnalysisResponse(BaseModel):
    behavioural: List[CompetencyCoverageResult] = Field(default_factory=list)
    functional: List[CompetencyCoverageResult] = Field(default_factory=list)
    domain: List[CompetencyCoverageResult] = Field(default_factory=list)

    behavioural_course_scores: List[CourseScoreResult] = Field(default_factory=list)
    functional_course_scores: List[CourseScoreResult] = Field(default_factory=list)
    domain_course_scores: List[CourseScoreResult] = Field(default_factory=list)
