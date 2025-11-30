from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
import uuid
from enum import Enum

# State Center Data Schemas
class StateCenterDocumentType(str, Enum):
    """Enum for Document types"""
    ACBP_DOC = "acbp_doc"
    WORK_DOC = "work_doc"

class StateCenterDataBase(BaseModel):
    """Base schema for State Center Data"""
    state_center_id: str = Field(..., description="ID of the associated state/center")
    department_id: Optional[str] = Field(None, description="ID of the associated department (optional)")

class StateCenterDataCreate(StateCenterDataBase):
    """Schema for creating State Center Data with file uploads"""
    pass

class StateCenterDataResponse(StateCenterDataBase):
    """Schema for State Center Data response"""
    id: uuid.UUID = Field(..., description="Unique identifier")
    acbp_plan_filename: Optional[str] = Field(None, description="ACBP Plan PDF filename")
    work_allocation_filename: Optional[str] = Field(None, description="Work Allocation Order PDF filename")
    acbp_plan_summary: Optional[str] = Field(None, description="AI-generated summary of ACBP Plan")
    work_allocation_order_summary: Optional[str] = Field(None, description="AI-generated summary of Work Allocation Order")
    status: str
    error_message: Optional[str] = None
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    
    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            uuid.UUID: lambda v: str(v)
        }

# File Upload Response Schema
class FileUploadResponse(BaseModel):
    """Schema for file upload response"""
    message: str = Field(..., description="Upload status message")
    data: StateCenterDataResponse = Field(..., description="Created/updated state center data")

# Error schemas
class ErrorResponse(BaseModel):
    """Schema for error responses"""
    detail: str
    error_code: Optional[str] = None
