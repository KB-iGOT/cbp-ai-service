from pydantic import BaseModel, Field

# Authentication Schemas
class LoginRequest():
    """Schema for user login request"""
    username: str = Field(
        ..., 
        min_length=3, 
        max_length=255,
        description="Username or email address"
    )
    password: str = Field(
        ..., 
        min_length=8,
        description="User password"
    )

class TokenResponse(BaseModel):
    """Schema for token response"""
    access_token: str = Field(..., description="JWT access token")
    refresh_token: str = Field(..., description="JWT refresh token")
    token_type: str = Field(default="bearer", description="Token type")
    expires_in: int = Field(..., description="Access token expiry time in seconds")

class RefreshTokenRequest(BaseModel):
    """Schema for refresh token request"""
    refresh_token: str = Field(..., description="JWT refresh token")

class RefreshTokenResponse(BaseModel):
    """Schema for refresh token response"""
    access_token: str = Field(..., description="New JWT access token")
    token_type: str = Field(default="bearer", description="Token type")
    expires_in: int = Field(..., description="Access token expiry time in seconds")

class LogoutResponse(BaseModel):
    """Schema for logout response"""
    message: str = Field(..., description="Logout confirmation message")
