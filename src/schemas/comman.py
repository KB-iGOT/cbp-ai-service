import enum
from datetime import date

from pydantic import BaseModel, Field, model_validator


class Competency(BaseModel):
    """Schema for competency"""
    type: str = Field(..., description="Type of competency (Behavioral, Functional, Domain)")
    theme: str = Field(..., description="Theme of the competency")
    sub_theme: str = Field(..., description="Sub-theme of the competency")


class ApprovalStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


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