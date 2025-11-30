from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """
    Application settings loaded from environment variables or a .env file.
    """
    LOG_LEVEL: str = "INFO"

    APP_NAME: str = "AI-Driven CBP Training Plan Creation System"
    APP_DESC: str = "API for managing state centers and training organizations for competency-based program development"
    APP_VERSION: str = "1.0.0"
    APP_ROOT_PATH: str = "/cbp-tpc-ai"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str

    GOOGLE_PROJECT_LOCATION: str
    GOOGLE_APPLICATION_CREDENTIALS: str
    EMBEDDING_MODEL_NAME: str = "text-multilingual-embedding-002"
    GOOGLE_PROJECT_ID: str

    KB_BASE_URL:str
    KB_AUTH_TOKEN:str

     # File upload settings
    PDF_MAX_FILE_SIZE: int = 50 * 1024 * 1024  # 50MB in bytes
    CSV_MAX_FILE_SIZE: int = 10 * 1024 * 1024  # 10MB in bytes
    ALLOWED_FILE_TYPES: list = [".pdf"]

    # Document storage settings
    DOCUMENT_STORAGE_TYPE: str = Field(
        default="local",
        description="Storage type: 'local' or 'gcp'"
    )
    DOCUMENT_STORAGE_ROOT: str = Field(
        default="storage/documents",
        description="Root directory (relative or absolute) where uploaded documents are stored (for local storage)"
    )
    
    # GCP Storage settings (used when DOCUMENT_STORAGE_TYPE=gcp)
    GCP_STORAGE_BUCKET: str = Field(
        default="",
        description="GCP Storage bucket name for document storage"
    )
    GCP_STORAGE_PREFIX: str = Field(
        default="documents",
        description="Prefix path within GCP bucket for organizing documents"
    )
    GCP_STORAGE_CREDENTIALS: str = Field(
        default="",
        description="Path to GCP service account JSON file for Cloud Storage (separate from Gemini AI credentials)"
    )

    # JWT Authentication settings
    SECRET_KEY: str = Field(
        ...,
        description="Secret key for JWT token signing"
    )
    ALGORITHM: str = Field(
        default="HS256",
        description="Algorithm for JWT token signing"
    )
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(
        default=1440,
        description="Access token expiry time in minutes"
    )
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(
        default=7,
        description="Refresh token expiry time in days"
    )
    
    # Optional: Token blacklist settings (for logout functionality)
    ENABLE_TOKEN_BLACKLIST: bool = Field(
        default=False,
        description="Enable token blacklisting for logout"
    )

# Create a settings instance that can be imported by other modules
settings = Settings()