from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
import uuid



class DocumentResponse(BaseModel):
    file_id: uuid.UUID
    filename: str
    document_name: Optional[str] = None
    uploader_id: Optional[uuid.UUID] = None
    state_center_id: str
    department_id: Optional[str]
    summary_status: str
    last_summary_request_id: Optional[uuid.UUID] = None
    summary_text: Optional[str] = None
    summary_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        json_encoders = {datetime: lambda v: v.isoformat(), uuid.UUID: lambda v: str(v)}


class UploadFailure(BaseModel):
    filename: str
    error: str

class BatchUploadResponse(BaseModel):
    status: str  # "complete", "partial", "failed"
    message: str
    total_files: int
    successful_count: int
    failed_count: int
    successful_uploads: List[DocumentResponse]
    failed_uploads: List[UploadFailure]

class DocumentListResponse(BaseModel):
    items: List[DocumentResponse]
    total: int

class SummaryTriggerResponse(BaseModel):
    file_id: uuid.UUID
    request_id: uuid.UUID
    summary_status: str

class SummaryDeleteResponse(BaseModel):
    message: str
    file_id: uuid.UUID
    filename: str

class DocumentDeleteResponse(BaseModel):
    message: str
    file_id: uuid.UUID
    filename: str
    deleted_meta_summaries: List[str] = Field(default=[], description="List of meta-summary request IDs that were deleted because they only referenced this file")
