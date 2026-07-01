from enum import Enum
from typing import Union
from pydantic import Field, field_validator
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
    REDIS_HOST: str = Field(default="localhost", description="Redis host")
    REDIS_PORT: int = Field(default=6379, description="Redis port")
    REDIS_DB: int = Field(default=0, description="Redis database index")
    REDIS_DESIG_EMB_PREFIX: str = Field(default="cbp_desig_emb", description="Redis key prefix for designation embeddings cache")
    DESIGNATION_SIMILARITY_THRESHOLD: float = Field(default=0.92, description="Minimum cosine similarity score for semantic designation match")
    DESIGNATION_EMBED_BATCH_SIZE: int = Field(default=100, description="Max designations per Gemini embedding API call")

    @property
    def REDIS_URL(self) -> str:
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    GOOGLE_PROJECT_LOCATION: str
    GOOGLE_APPLICATION_CREDENTIALS: str
    GOOGLE_API_KEY: str
    GOOGLE_EMBEDDING_MODEL: str = Field(default="gemini-embedding-2", description="Gemini embedding model name")
    EMBEDDING_OUTPUT_DIMENSIONALITY: int = Field(default=1536, description="Output dimensionality for Gemini embedding model")
    # Legacy embedding model for the optional content_rerank layer only. content_embeddings
    # was built in this model's 768-dim space, which is DIFFERENT from GOOGLE_EMBEDDING_MODEL
    # (gemini-embedding-2, 1536). content_rerank embeds the query with this model separately.
    CONTENT_EMBEDDING_MODEL: str = Field(default="text-multilingual-embedding-002", description="Legacy embedding model matching the content_embeddings vector space (used by content_rerank)")
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
    BHASHINI_SUPPORTED_LANGUAGES: Union[str, list[str]] = Field(
        default="en,hi,te,kn,mr,ta,gu,ml,or,pa,bn,as",
        description="Supported language codes for translation (comma-separated or list)"
    )

    @field_validator("BHASHINI_SUPPORTED_LANGUAGES", mode="after")
    @classmethod
    def parse_langs(cls, v):
        if isinstance(v, str):
            # Handle comma-separated string from .env file
            return [x.strip() for x in v.split(",") if x.strip()]
        elif isinstance(v, list):
            return v
        return v
    
    DEFAULT_RELEVANCY_SCORE: int = 90

    # Notification service settings
    ENABLE_EMAIL_NOTIFICATION: bool = Field(
        default=False,
        description="Enable or disable email notifications (disabled by default)"
    )
    NOTIFICATION_BASE_URL: str = Field(
        default="",
        description="Base URL for the notification service"
    )
    SPV_PORTAL_URL: str = Field(
        default="",
        description="SPV Portal URL for review links"
    )
    MDO_PORTAL_URL: str = Field(
        default="",
        description="MDO Portal URL for CBP review links"
    )

    # --- Course-recommendation retrieval tuning (feature flags) ---------------
    HYBRID_SEARCH_ENABLED: bool = Field(default=True, description="Hybrid dense (semantic) + lexical (full-text) retrieval fused with RRF")
    COMM_GENAI_BOOST_ENABLED: bool = Field(default=True, description="Gently boost Communication / GenAI courses among relevant results")
    COMPETENCY_BOOST_ENABLED: bool = Field(default=True, description="Boost courses whose competencies overlap the role's competencies")
    CONTENT_RERANK_ENABLED: bool = Field(default=True, description="Content-embedding rerank layer between hybrid search and the LLM")
    CONTENT_RERANK_TOP_N: int = Field(default=40, description="Candidates kept after content rerank (passed to the LLM)")
    CONTENT_RERANK_WEIGHT: float = Field(default=0.5, description="Blend weight: (1-w)*hybrid + w*content")

# Create a settings instance that can be imported by other modules
settings = Settings()