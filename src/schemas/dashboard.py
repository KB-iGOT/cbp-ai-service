from pydantic import BaseModel, Field, model_validator
from typing import List, Optional
from datetime import date

class DateRange(BaseModel):
    from_date: date = Field(alias="from")
    to_date: date = Field(alias="to")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_dates(self) -> "DateRange":
        today = date.today()

        if self.from_date > today:
            raise ValueError(f"'from' date ({self.from_date}) cannot be a future date.")
        if self.to_date > today:
            raise ValueError(f"'to' date ({self.to_date}) cannot be a future date.")
        if self.from_date > self.to_date:
            raise ValueError(
                f"'from' date ({self.from_date}) must be before or equal to 'to' date ({self.to_date})."
            )
        return self


class CBPSummaryTrendFilters(BaseModel):
    state_center_id: Optional[str] = None
    department_org_ids: List[str] = []
    date_range: Optional[DateRange] = None
    trend_granularity: str = "Monthly"

class CBPSummaryTrendRequest(BaseModel):
    filters: CBPSummaryTrendFilters


class TrendPoint(BaseModel):
    period: str
    cbp_count: int

class CBPSummaryTrendResponse(BaseModel):
    state_center_id: str
    state_center_name: str
    department_org_name: Optional[str]
    trend: List[TrendPoint]


class CBPDashboardFilters(BaseModel):
    ministries: Optional[List[str]] = None
    departments: Optional[List[str]] = None
    date_range: Optional[DateRange] = None

class CBPDashboardMetricsResponse(BaseModel):
    total_users: int
    users_with_role_mappings: int
    total_role_mappings: int
    unique_role_mappings: int
    role_mappings_with_recommendations: int
    saved_recommended_courses_count: int
    ministry_count: int
    department_count: int
    total_documents: int
    total_cbp_plan_count: int
    behavioral_competencies_count: int
    functional_competencies_count: int
    domain_competencies_count: int


class GapAnalysisFilters(BaseModel):
    ministries: Optional[List[str]] = None
    departments: Optional[List[str]] = None
    date_range: Optional[DateRange] = None


class GapAnalysisResponse(BaseModel):
    competencies_without_courses: int          
    behavioral_without_courses: int
    functional_without_courses: int
    domain_without_courses: int


# ── User-scoped dashboard schemas (for regular/public users) ──────────────────

class UserDashboardFilters(BaseModel):
    """Filters for user-level dashboard — only date range, user_id comes from JWT token."""
    date_range: Optional[DateRange] = None


class UserDashboardMetricsResponse(BaseModel):
    """Metrics scoped to the logged-in user's own role mappings and CBP plans."""
    total_role_mappings: int
    unique_role_mappings: int
    role_mappings_with_recommendations: int
    saved_recommended_courses_count: int
    total_cbp_plan_count: int
    behavioral_competencies_count: int
    functional_competencies_count: int
    domain_competencies_count: int


class UserGapAnalysisResponse(BaseModel):
    """Gap analysis scoped to the logged-in user's own role mappings."""
    competencies_without_courses: int
    behavioral_without_courses: int
    functional_without_courses: int
    domain_without_courses: int
