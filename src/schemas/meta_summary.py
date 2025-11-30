from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
import uuid

class MetaSummaryCreateRequest(BaseModel):
    file_ids: List[uuid.UUID] = Field(..., min_items=1, description="List of document IDs to aggregate")

class MetaSummaryResponse(BaseModel):
    request_id: uuid.UUID
    state_center_id: str
    department_id: Optional[str]
    status: str
    file_ids: List[uuid.UUID]
    summary_text: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        json_encoders = {datetime: lambda v: v.isoformat(), uuid.UUID: lambda v: str(v)}

class MetaSummaryListItem(BaseModel):
    request_id: uuid.UUID
    state_center_id: str
    department_id: Optional[str]
    status: str
    file_ids: List[uuid.UUID] = Field(..., description="Document IDs included in this meta-summary")
    summary_text: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        json_encoders = {datetime: lambda v: v.isoformat(), uuid.UUID: lambda v: str(v)}

class MetaSummaryListResponse(BaseModel):
    items: Optional[List[MetaSummaryListItem]]
    total: int


class MetaSummaryDeleteResponse(BaseModel):
    message: str
    request_id: uuid.UUID
