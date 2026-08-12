"""The native google-genai adapter — the only file in this codebase allowed to import
`google.genai`. Talks Vertex AI for generation (matches the config-driven client that
src/services/v3/role_mapping_service.py already built correctly) and API-key mode for
embeddings (matches designation_matcher_service/course_recommendation's embedding client)."""
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Sequence

from google import genai
from google.genai import types as gtypes
from pydantic import BaseModel

from ....core.configs import settings
from ....core.logger import logger
from ..base import Capability, EmbeddingProvider, LLMProvider
from ..models import resolve_model, split_provider
from ..types import (
    CacheHandle,
    GenerationConfig,
    LLMResponse,
    Message,
    Part,
    Role,
    SafetyPolicy,
    Tool,
    Usage,
)

_GOOGLE_PROVIDER_PREFIXES = {"google_genai", "google_vertexai", "gemini", "google"}


def _gemini_model(model: str) -> str:
    """Apply LLM_MODEL_MAP, then strip any LangChain-style provider prefix, which this native
    SDK does not understand. A prefix naming a non-Google provider means the map is pointed at
    another vendor while LLM_PROVIDER is still `gemini` — warn rather than send a model name
    that will 404, since the mismatch is a config error, not a transient failure."""
    resolved = resolve_model(model)
    prefix, bare = split_provider(resolved)
    if prefix and prefix not in _GOOGLE_PROVIDER_PREFIXES:
        logger.warning(
            f"LLM_MODEL_MAP routes '{model}' to '{resolved}' (provider '{prefix}'), but "
            f"LLM_PROVIDER=gemini uses the native Google SDK. Sending '{bare}' to Gemini; set "
            f"LLM_PROVIDER=langchain to actually reach '{prefix}'."
        )
    return bare


_PERMISSIVE_SAFETY_SETTINGS = [
    gtypes.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
    gtypes.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
    gtypes.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
    gtypes.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
]


def _build_http_options() -> gtypes.HttpOptions:
    return gtypes.HttpOptions(
        retry_options=gtypes.HttpRetryOptions(
            initial_delay=settings.GEMINI_RETRY_INITIAL_DELAY,
            attempts=settings.GEMINI_RETRY_ATTEMPTS,
            exp_base=settings.GEMINI_RETRY_EXP_BASE,
            http_status_codes=settings.GEMINI_RETRY_HTTP_STATUS_CODES,
        )
    )


def _to_gemini_part(part: Part) -> gtypes.Part:
    if part.data is not None:
        return gtypes.Part.from_bytes(data=part.data, mime_type=part.mime_type or "application/octet-stream")
    return gtypes.Part.from_text(text=part.text or "")


def _to_gemini_contents(contents: str | Sequence[Message]) -> list[gtypes.Content]:
    if isinstance(contents, str):
        return [gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=contents)])]
    return [
        gtypes.Content(
            role="model" if message.role == Role.MODEL else "user",
            parts=[_to_gemini_part(p) for p in message.parts],
        )
        for message in contents
    ]


def _to_gemini_schema(schema: Any) -> Any:
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema()
    return schema


def _extract_usage(response: Any) -> Usage:
    meta = getattr(response, "usage_metadata", None)
    if not meta:
        return Usage()
    return Usage(
        input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
        output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
        thinking_tokens=getattr(meta, "thoughts_token_count", 0) or 0,
        cached_tokens=getattr(meta, "cached_content_token_count", 0) or 0,
        total_tokens=getattr(meta, "total_token_count", 0) or 0,
    )


def _build_gen_kwargs(config: GenerationConfig) -> dict:
    kwargs: dict = {}
    if config.temperature is not None:
        kwargs["temperature"] = config.temperature
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.top_k is not None:
        kwargs["top_k"] = config.top_k
    if config.max_output_tokens is not None:
        kwargs["max_output_tokens"] = config.max_output_tokens
    if config.seed is not None:
        kwargs["seed"] = config.seed
    if config.system_instruction:
        kwargs["system_instruction"] = config.system_instruction
    if config.json_output or config.response_schema is not None:
        kwargs["response_mime_type"] = "application/json"
        if config.response_schema is not None:
            kwargs["response_schema"] = _to_gemini_schema(config.response_schema)
    if config.safety == SafetyPolicy.PERMISSIVE:
        kwargs["safety_settings"] = _PERMISSIVE_SAFETY_SETTINGS
    if config.thinking_budget is not None:
        kwargs["thinking_config"] = gtypes.ThinkingConfig(
            thinking_budget=config.thinking_budget, include_thoughts=config.include_thoughts
        )
    if Tool.WEB_SEARCH in config.tools:
        kwargs["tools"] = [{"google_search": {}}]
    if config.cache and config.cache.name:
        kwargs["cached_content"] = config.cache.name
    kwargs.update(config.provider_options.get("gemini", {}))
    return kwargs


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS
        self._client = genai.Client(
            project=settings.GOOGLE_PROJECT_ID,
            location=settings.GOOOGLE_PROJECT_LOCATION_GLOBAL,
            vertexai=settings.GOOGLE_GENAI_USE_VERTEXAI,
            http_options=_build_http_options(),
        )
        logger.info("GeminiProvider initialized (Vertex AI)")

    def supports(self, capability: Capability) -> bool:
        return capability in {
            Capability.STREAMING,
            Capability.NATIVE_JSON_SCHEMA,
            Capability.CONTEXT_CACHE,
            Capability.NATIVE_FILE_INPUT,
            Capability.WEB_SEARCH,
            Capability.THINKING,
            Capability.TOKEN_COUNT,
        }

    async def generate(
        self,
        contents: str | Sequence[Message],
        *,
        model: str,
        config: GenerationConfig | None = None,
    ) -> LLMResponse:
        config = config or GenerationConfig()
        model = _gemini_model(model)
        start = time.monotonic()
        response = await self._client.aio.models.generate_content(
            model=model,
            contents=_to_gemini_contents(contents),
            config=gtypes.GenerateContentConfig(**_build_gen_kwargs(config)),
        )
        latency_ms = (time.monotonic() - start) * 1000
        usage = _extract_usage(response)
        self._record_usage(model=model, usage=usage, latency_ms=latency_ms)
        return LLMResponse(text=response.text or "", usage=usage, model=model, provider=self.name, raw=response)

    async def stream(
        self,
        contents: str | Sequence[Message],
        *,
        model: str,
        config: GenerationConfig | None = None,
    ) -> AsyncIterator[str]:
        config = config or GenerationConfig()
        model = _gemini_model(model)
        result = await self._client.aio.models.generate_content_stream(
            model=model,
            contents=_to_gemini_contents(contents),
            config=gtypes.GenerateContentConfig(**_build_gen_kwargs(config)),
        )
        async for chunk in result:
            if chunk.text:
                yield chunk.text

    async def count_tokens(self, contents: str | Sequence[Message], *, model: str) -> int:
        response = await self._client.aio.models.count_tokens(
            model=_gemini_model(model), contents=_to_gemini_contents(contents)
        )
        return response.total_tokens

    @asynccontextmanager
    async def cached_context(
        self, parts: Sequence[Part], *, model: str, ttl_seconds: int = 0
    ) -> AsyncIterator[CacheHandle]:
        parts = list(parts)
        if ttl_seconds <= 0 or not parts:
            yield CacheHandle(name=None, parts=parts)
            return

        cache_name = None
        try:
            cache = await self._client.aio.caches.create(
                model=_gemini_model(model),
                config=gtypes.CreateCachedContentConfig(
                    display_name="cbp-ai-service-cache",
                    contents=[gtypes.Content(role="user", parts=[_to_gemini_part(p) for p in parts])],
                    ttl=f"{ttl_seconds}s",
                ),
            )
            cache_name = cache.name
            logger.info(f"Gemini context cache created ({cache_name}, ttl={ttl_seconds}s)")
        except Exception as e:
            logger.warning(f"Gemini context cache unavailable ({e}); sending content inline")

        if cache_name:
            try:
                yield CacheHandle(name=cache_name, parts=[])
            finally:
                try:
                    await self._client.aio.caches.delete(name=cache_name)
                except Exception as e:
                    logger.warning(f"Gemini cache delete failed for {cache_name}: {e} (will expire via TTL)")
        else:
            yield CacheHandle(name=None, parts=parts)


class GeminiEmbeddingProvider(EmbeddingProvider):
    name = "gemini"

    def __init__(self):
        self._client = genai.Client(
            api_key=settings.GOOGLE_API_KEY,
            vertexai=False,
            http_options=_build_http_options(),
        )
        logger.info("GeminiEmbeddingProvider initialized (API key)")

    async def embed_batch(
        self,
        texts: Sequence[str],
        *,
        model: str,
        dimensions: int | None = None,
        task_type: str | None = None,
    ) -> list[list[float]]:
        config = gtypes.EmbedContentConfig(output_dimensionality=dimensions) if dimensions else None
        response = await self._client.aio.models.embed_content(
            model=_gemini_model(model), contents=list(texts), config=config
        )
        return [e.values for e in response.embeddings]
