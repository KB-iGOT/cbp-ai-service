import enum

from pydantic import BaseModel, Field


class Competency(BaseModel):
    """Schema for competency"""
    type: str = Field(..., description="Type of competency (Behavioral, Functional, Domain)")
    theme: str = Field(..., description="Theme of the competency")
    sub_theme: str = Field(..., description="Sub-theme of the competency")


class ApprovalStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING = "pending"
    PUBLISHED = "published"
    REJECTED = "rejected"