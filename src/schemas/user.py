from pydantic import BaseModel, EmailStr, Field
from typing import Any, Dict, List, Optional
from datetime import datetime
import uuid

class UserBase(BaseModel):
    """Base user schema"""
    username: str = Field(..., min_length=3, max_length=100)
    email: EmailStr
    phone: Optional[str] = Field(None, max_length=20)
    role_id: uuid.UUID
    state_center_id: Optional[str] = Field(None)
    department_id: Optional[str] = Field(None)
    organization_ids: List[str] = [] 
    is_active: bool = Field(default=True)

class UserCreate(UserBase):
    """Schema for creating a user"""
    password: str = Field(..., min_length=8, description="Password (min 8 characters)")
    created_by: Optional[uuid.UUID] = Field(None)

class UserUpdate(BaseModel):
    """Schema for updating a user"""
    username: Optional[str] = Field(None, min_length=3, max_length=100)
    email: Optional[EmailStr] = Field(None)
    phone: Optional[str] = Field(None, max_length=20)
    role_id: Optional[uuid.UUID] = Field(None)
    state_center_id: Optional[str] = Field(None)
    department_id: Optional[str] = Field(None)
    is_active: Optional[bool] = Field(None)
    organization_ids: List[str] = []

class UserResponse(UserBase):
    """Schema for user response"""
    user_id: uuid.UUID
    last_login: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    role_info: Optional[Dict[str, Any]] = Field(None, description="Role details")
    creator_info: Optional[Dict[str, str]] = Field(None, description="Creator details")
    
    class Config:
        from_attributes = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            uuid.UUID: lambda v: str(v)
        }

class PaginatedUserResponse(BaseModel):
    total: int
    limit: int
    offset: int
    data: List[UserResponse]
