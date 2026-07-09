import uuid
from typing import List, Optional
from pydantic import BaseModel, Field


class CoverageAnalysisRequest(BaseModel):
    """Request body — only an identifier. The designation's competency framework and its
    recommended courses (RecommendedCourse.filtered_courses) are fetched server-side from
    this role_mapping_id, scoped to the current user."""
    role_mapping_id: uuid.UUID = Field(..., description="ID of the role mapping to analyze")


class CourseScore(BaseModel):
    """One evidence course and its similarity score against a competency. Only populated for
    Domain rows (semantic similarity); empty for Behavioral/Functional (exact tag match)."""
    course_name: str
    score: float = Field(..., description="Cosine similarity 0-1")


class CompetencyCoverageRow(BaseModel):
    """One flat row per competency — the whole response is a list of these, so it converts
    directly into a table. Field semantics differ by competency_area:

      - Behavioral/Functional: match_type='exact_tag_match', matched=true/false (exact
        theme+sub_theme tag match), matching_score=None, matched_courses=all courses whose
        own KCM tag matches exactly, course_scores=[] (no scoring involved).
      - Domain: match_type='semantic_similarity', matched=None (no threshold — the caller
        decides what counts), matching_score=best cosine score, matched_courses=[best course],
        course_scores=ALL domain courses with their scores (sorted high→low)."""
    competency_area: str = Field(..., description="Behavioral | Functional | Domain")
    theme: str
    sub_theme: str
    competency: str = Field(..., description="theme - sub_theme")
    matched: Optional[bool] = Field(None, description="y/n for Behavioral/Functional; null for Domain")
    match_type: str = Field(..., description="exact_tag_match | semantic_similarity")
    matching_score: Optional[float] = Field(None, description="Best cosine score for Domain; null for B/F")
    matched_courses: List[str] = Field(default_factory=list)
    course_scores: List[CourseScore] = Field(default_factory=list)
    rationale: str


class CoverageAnalysisResponse(BaseModel):
    designation_name: str
    role_mapping_id: str
    state_center_name: Optional[str] = None
    department_name: Optional[str] = None
    rows: List[CompetencyCoverageRow] = Field(default_factory=list)
