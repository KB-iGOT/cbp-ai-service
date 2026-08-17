import json
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

class LLMProviderOption(str, Enum):
    GEMINI = "gemini"
    LANGCHAIN = "langchain"

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
    DESIGNATION_SIMILARITY_THRESHOLD: float = Field(default=0.93, description="Minimum cosine similarity score for semantic designation match")
    DESIGNATION_EMBED_BATCH_SIZE: int = Field(default=100, description="Max designations per Gemini embedding API call")

    @property
    def REDIS_URL(self) -> str:
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    GOOGLE_PROJECT_LOCATION: str
    GOOOGLE_PROJECT_LOCATION_GLOBAL: str = Field(default="global", description="Default location for Gemini AI services")
    GOOGLE_APPLICATION_CREDENTIALS: str
    GOOGLE_API_KEY: str
    GOOGLE_EMBEDDING_MODEL: str = Field(default="gemini-embedding-2", description="Gemini embedding model name")
    EMBEDDING_OUTPUT_DIMENSIONALITY: int = Field(default=1536, description="Output dimensionality for Gemini embedding model")
    GOOGLE_PROJECT_ID: str
    GOOGLE_GENAI_USE_VERTEXAI: bool = Field(default=True, description="Flag to use Vertex AI for Gemini API calls")
    GEMINI_PRO_MODEL_NAME: str = Field(default="gemini-3.1-pro-preview", description="Gemini Pro model name for advanced tasks")
    GEMINI_FLASH_MODEL_NAME: str = Field(default="gemini-3.5-flash", description="Gemini Flash model name for high-speed tasks")

    # Gemini API client retry settings
    GEMINI_RETRY_INITIAL_DELAY: float = Field(default=1.0, description="Initial delay (seconds) before the first retry of a failed Gemini API call")
    GEMINI_RETRY_ATTEMPTS: int = Field(default=3, description="Max number of retry attempts for a failed Gemini API call")
    GEMINI_RETRY_EXP_BASE: float = Field(default=2.0, description="Exponential backoff base multiplier for Gemini API call retries")
    GEMINI_RETRY_HTTP_STATUS_CODES: list[int] = Field(default=[429, 500, 502, 503, 504], description="HTTP status codes that trigger a retry for Gemini API calls")

    ROLE_MAPPING_BATCH_SIZE: int = Field(default=30, description="Number of designations per batch when processing PASS 2 role mapping generation in parallel")

    # LLM provider selection — swap the backing adapter without touching call sites.
    # See src/services/llm_service.py for the provider-agnostic interface.
    LLM_PROVIDER: LLMProviderOption = Field(default=LLMProviderOption.GEMINI, description="Provider backing src.services.llm_service.get_llm()")
    LLM_EMBEDDING_PROVIDER: LLMProviderOption = Field(default=LLMProviderOption.GEMINI, description="Provider backing src.services.llm_service.get_embedder()")

    # Model redirection map, applied by src/services/llm_service.py to EVERY model name before
    # it reaches a provider. Exists because several call sites pass a hardcoded Gemini model
    # literal (e.g. "gemini-2.5-pro") rather than reading a setting, so those calls could not
    # otherwise follow a provider switch. Values may carry a LangChain "provider:model" prefix.
    #   LLM_MODEL_MAP='{"gemini-2.5-pro": "openai:gpt-5", "gemini-3.5-flash": "openai:gpt-5-mini"}'
    # Empty (the default) means no redirection — every model name passes through untouched, so
    # behaviour is identical to having no map at all.
    LLM_MODEL_MAP: Union[str, dict[str, str]] = Field(
        default_factory=dict,
        description="JSON object (or dict) remapping model names before they reach the provider. "
                    "Empty means pass-through."
    )

    @field_validator("LLM_MODEL_MAP", mode="after")
    @classmethod
    def parse_model_map(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return {}
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError as e:
                raise ValueError(f"LLM_MODEL_MAP must be a JSON object: {e}") from e
            if not isinstance(parsed, dict):
                raise ValueError("LLM_MODEL_MAP must be a JSON object, not a list/scalar")
            return {str(k): str(val) for k, val in parsed.items()}
        return v or {}

    # API keys for non-Google providers reached through the LangChain adapter. Only the key for
    # the provider actually in use needs to be set; the adapter exports it to the environment
    # that the corresponding LangChain integration package reads.
    OPENAI_API_KEY: str = Field(default="", description="Required only when routing to openai:* models")
    ANTHROPIC_API_KEY: str = Field(default="", description="Required only when routing to anthropic:* models")

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

    COURSE_RECOMMENDATION_MIN_RELEVANCY: int = 80

    # Per-competency-type controls for course recommendation. Behavioural/Functional courses are
    # generic and naturally score lower relevancy than Domain courses, so a single flat cutoff
    # silently deletes them. These give each type its own relevancy floor and a hard MINIMUM count
    # enforced deterministically after LLM filtering (top-up from the already-retrieved pool when a
    # type is under its minimum) so per-type counts are stable run-to-run. There is no maximum /
    # trim: a course the LLM selected is never dropped to satisfy a quota.
    ENFORCE_COMPETENCY_QUOTAS: bool = Field(
        default=True,
        description="If true, enforce hard per-type minimum counts on the final recommended "
                    "courses (deterministic top-up, never a trim). Set false to fall back to the "
                    "flat COURSE_RECOMMENDATION_MIN_RELEVANCY cutoff with no count guarantees."
    )
    # Public/external course lookup via the provider's builtin web search. Disabled by default,
    # matching the long-standing "Disabled for temporary reasons" early-return this replaces —
    # enabling it adds a web-search LLM call per recommendation and mixes public (non-iGOT)
    # courses into results.
    ENABLE_GENERAL_COURSE_LOOKUP: bool = Field(
        default=False,
        description="If true, also recommend public courses found via provider web search."
    )

    BEHAVIOURAL_MIN_RELEVANCY: int = 75
    FUNCTIONAL_MIN_RELEVANCY: int = 75
    BEHAVIOURAL_MIN_COUNT: int = 3
    FUNCTIONAL_MIN_COUNT: int = 3
    # Domain keeps the original COURSE_RECOMMENDATION_MIN_RELEVANCY (80) floor — no separate,
    # lower Domain floor — so Domain courses are filtered exactly as they always were. Only the
    # count is now guaranteed, because Domain was observed swinging (occasionally near zero)
    # run-to-run for the same role profile.
    DOMAIN_MIN_COUNT: int = 3

    # Domain competencies from the Work Allocation Order (WAO)
    DOMAIN_FROM_WAO_ENABLED: bool = Field(
        default=False,
        description="If true, derive Domain competencies per designation directly from the raw WAO "
                    "text (uncapped, exhaustive) instead of the summary-based capped list. "
                    "Behavioural/Functional competencies are unchanged. Falls back silently to the "
                    "existing behaviour if the raw WAO cannot be read."
    )
    DOMAIN_FROM_WAO_CONCURRENCY: int = Field(
        default=4,
        description="Max concurrent per-designation domain-from-WAO LLM calls."
    )
    DOMAIN_FROM_WAO_MIN: int = Field(
        default=6,
        description="Minimum number of Domain competencies to keep per designation. If the WAO "
                    "yields fewer, the shortfall is topped up (deduplicated) from the summary-based "
                    "set rather than dropping below the floor or padding with invented items. "
                    "Set to 0 to disable the floor (use exactly what the WAO supports)."
    )
    DOMAIN_FROM_WAO_CACHE_TTL_SECONDS: int = Field(
        default=600,
        description="TTL (seconds) for the Gemini context cache holding the WAO PDF, so the document "
                    "is uploaded/charged once and reused across the per-designation domain calls. "
                    "Set to 0 to disable caching (the PDF is sent inline on each call)."
    )

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

# Create a settings instance that can be imported by other modules
settings = Settings()