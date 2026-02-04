from enum import Enum
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class EnvironmentOption(str, Enum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"

class DocumentStorageOption(str, Enum):
    LOCAL = "local"
    GCP = "gcp"

class Settings(BaseSettings):
    """
    Application settings loaded from environment variables or a .env file.
    """
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENVIRONMENT: EnvironmentOption = EnvironmentOption.LOCAL
    LOG_LEVEL: str = "INFO"

    APP_NAME: str = "AI-Driven CBP Training Plan Creation System"
    APP_DESC: str = "API for managing state centers and training organizations for competency-based program development"
    APP_VERSION: str = "1.0.0"
    APP_ROOT_PATH: str = "/cbp-tpc-ai"

    DATABASE_URL: str

    GOOGLE_PROJECT_LOCATION: str
    GOOGLE_APPLICATION_CREDENTIALS: str
    EMBEDDING_MODEL_NAME: str = "text-multilingual-embedding-002"
    GOOGLE_PROJECT_ID: str

    KB_BASE_URL: str
    KB_AUTH_TOKEN: str

     # File upload settings
    PDF_MAX_FILE_SIZE: int = 50 * 1024 * 1024  # 50MB in bytes
    CSV_MAX_FILE_SIZE: int = 10 * 1024 * 1024  # 10MB in bytes
    ALLOWED_FILE_TYPES: list = [".pdf"]

    # Document storage settings
    DOCUMENT_STORAGE_TYPE: DocumentStorageOption = DocumentStorageOption.LOCAL
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
        default=True,
        description="Enable token blacklisting for logout"
    )

    # Login attempt settings (brute-force protection)
    MAX_LOGIN_ATTEMPTS: int = Field(
        default=5,
        description="Maximum failed login attempts before account lockout"
    )
    LOGIN_LOCKOUT_MINUTES: int = Field(
        default=15,
        description="Account lockout duration in minutes after max failed attempts"
    )

    # Bhashini Translation API settings
    BHASHINI_USER_ID: str = Field(
        default="",
        description="Bhashini API User ID for translation services"
    )
    BHASHINI_API_KEY: str = Field(
        default="",
        description="Bhashini API Key for translation services"
    )
    BHASHINI_PIPELINE_ID: str = Field(
        default="64392f96daac500b55c543cd",
        description="Bhashini Pipeline ID for translation services"
    )
    BHASHINI_CONFIG_API_URL: str = Field(
        default="https://meity-auth.ulcacontrib.org/ulca/apis/v0/model/getModelsPipeline",
        description="Bhashini Config API URL for getting translation configuration"
    )
    BHASHINI_SUPPORTED_LANGUAGES: list = Field(
        default=["en", "hi", "te", "kn", "mr", "ta", "gu", "ml", "or", "pa", "bn", "as"],
        description="Supported language codes for translation"
    )

# Create a settings instance that can be imported by other modules
settings = Settings()