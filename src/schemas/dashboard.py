from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import date

class DateRange(BaseModel):
    from_date: date = Field(alias="from")  
    to_date: date = Field(alias="to")      

    model_config = {"populate_by_name": True}  # <-- Fix: Pydantic v2 config


class CBPSummaryTrendFilters(BaseModel):
    state_center_id: str = None
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
