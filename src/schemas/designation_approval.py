import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class DesignationApprovalCreate(BaseModel):
    rolemapping_id: uuid.UUID = Field(..., description="ID of the unmatched role mapping")
    designation_name: str = Field(..., min_length=1, max_length=255, description="Designation name")
    wing_division_section: str = Field(..., min_length=1, max_length=255, description="Division/Wing name")


class DesignationApprovalResponse(BaseModel):
    id: uuid.UUID
    rolemapping_id: uuid.UUID
    user_id: uuid.UUID
    designation_name: str
    wing_division_section: str
    status: str
    reviewer_comments: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            uuid.UUID: lambda v: str(v),
        }


class DesignationApprovalListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    requests: list[DesignationApprovalResponse]
