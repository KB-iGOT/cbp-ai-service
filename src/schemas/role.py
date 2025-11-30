from pydantic import BaseModel, Field
from typing import Any, Dict, Optional
from datetime import datetime
import uuid

# Role Schemas
class RoleBase(BaseModel):
    """Base role schema"""
    role_name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None)
    permissions: Optional[Dict[str, Any]] = Field(default_factory=dict)
    is_active: bool = Field(default=True)

class RoleCreate(RoleBase):
    """Schema for creating a role"""
    pass

class RoleUpdate(BaseModel):
    """Schema for updating a role"""
    role_name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None)
    permissions: Optional[Dict[str, Any]] = Field(None)
    is_active: Optional[bool] = Field(None)

class RoleResponse(RoleBase):
    """Schema for role response"""
    role_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            uuid.UUID: lambda v: str(v)
        }
