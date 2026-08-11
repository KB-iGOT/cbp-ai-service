"""Config-driven factory — the one place that decides which adapter backs the port.
Mirrors get_storage_service() in services/storage_service.py."""
from functools import lru_cache

from ...core.configs import LLMProviderOption, settings
from .base import EmbeddingProvider, LLMProvider


@lru_cache(maxsize=None)
def get_llm(provider: LLMProviderOption | None = None) -> LLMProvider:
    provider = provider or settings.LLM_PROVIDER
    if provider == LLMProviderOption.GEMINI:
        from .providers.gemini import GeminiProvider
        return GeminiProvider()
    if provider == LLMProviderOption.LANGCHAIN:
        from .providers.langchain import LangChainProvider
        return LangChainProvider()
    raise ValueError(f"Unknown LLM provider: {provider}")


@lru_cache(maxsize=None)
def get_embedder(provider: LLMProviderOption | None = None) -> EmbeddingProvider:
    provider = provider or settings.LLM_EMBEDDING_PROVIDER
    if provider == LLMProviderOption.GEMINI:
        from .providers.gemini import GeminiEmbeddingProvider
        return GeminiEmbeddingProvider()
    if provider == LLMProviderOption.LANGCHAIN:
        from .providers.langchain import LangChainEmbeddingProvider
        return LangChainEmbeddingProvider()
    raise ValueError(f"Unknown embedding provider: {provider}")
