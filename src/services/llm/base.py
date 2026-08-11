"""The port: LLMProvider / EmbeddingProvider ABCs that every adapter implements, and that
all application code depends on instead of a provider SDK."""
import json
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, AsyncIterator, Sequence

from pydantic import BaseModel

from .errors import LLMEmptyResponseError, LLMSchemaError
from .types import CacheHandle, GenerationConfig, LLMResponse, Message, Part, Usage
from .usage import UsageRecord, emit_usage


class Capability(str, Enum):
    STREAMING = "streaming"
    NATIVE_JSON_SCHEMA = "native_json_schema"
    CONTEXT_CACHE = "context_cache"
    NATIVE_FILE_INPUT = "native_file_input"
    WEB_SEARCH = "web_search"
    THINKING = "thinking"
    TOKEN_COUNT = "token_count"


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def supports(self, capability: Capability) -> bool: ...

    @abstractmethod
    async def generate(
        self,
        contents: str | Sequence[Message],
        *,
        model: str,
        config: GenerationConfig | None = None,
    ) -> LLMResponse: ...

    @abstractmethod
    def stream(
        self,
        contents: str | Sequence[Message],
        *,
        model: str,
        config: GenerationConfig | None = None,
    ) -> AsyncIterator[str]: ...

    async def generate_structured(
        self,
        contents: str | Sequence[Message],
        *,
        model: str,
        schema: Any,
        config: GenerationConfig | None = None,
    ) -> Any:
        """Generate JSON constrained to `schema` (a pydantic BaseModel subclass, or a raw
        JSON-schema-shaped dict) and parse it. One repair retry on an empty or invalid
        response — this is what lets a future adapter with no native schema support still
        work correctly; Gemini's `generate` already constrains natively via response_schema,
        so the retry path is rarely exercised there."""
        base_config = (config or GenerationConfig()).copy(json_output=True, response_schema=schema)
        is_model = isinstance(schema, type) and issubclass(schema, BaseModel)

        attempt_contents = contents
        last_error: Exception | None = None
        for attempt in range(2):
            response = await self.generate(attempt_contents, model=model, config=base_config)
            text = response.text
            if not text:
                last_error = LLMEmptyResponseError(f"Empty response from {self.name}/{model}")
            else:
                try:
                    data = json.loads(text)
                    return schema.model_validate(data) if is_model else data
                except Exception as e:
                    last_error = e
            if attempt == 0:
                repair = Message.user(
                    f"Your previous output was empty or invalid ({last_error}). "
                    "Return ONLY valid JSON matching the required schema."
                )
                attempt_contents = [Message.user(contents)] if isinstance(contents, str) else list(contents)
                attempt_contents = attempt_contents + [repair]

        raise LLMSchemaError(
            f"{self.name}/{model} did not return valid JSON for schema after retry: {last_error}"
        )

    async def count_tokens(self, contents: str | Sequence[Message], *, model: str) -> int:
        raise NotImplementedError(f"{self.name} does not support token counting")

    @asynccontextmanager
    async def cached_context(
        self, parts: Sequence[Part], *, model: str, ttl_seconds: int = 0
    ) -> AsyncIterator[CacheHandle]:
        """Fallback for providers with no native context caching (Capability.CONTEXT_CACHE):
        hands the parts straight back for the caller to inline on every call."""
        yield CacheHandle(name=None, parts=list(parts))

    def _record_usage(self, *, model: str, usage: Usage | None, latency_ms: float, label: str | None = None) -> None:
        emit_usage(UsageRecord(
            provider=self.name,
            model=model,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            latency_ms=latency_ms,
            label=label,
        ))


class EmbeddingProvider(ABC):
    name: str

    @abstractmethod
    async def embed_batch(
        self,
        texts: Sequence[str],
        *,
        model: str,
        dimensions: int | None = None,
        task_type: str | None = None,
    ) -> list[list[float]]: ...

    async def embed(
        self,
        texts: Sequence[str],
        *,
        model: str,
        dimensions: int | None = None,
        task_type: str | None = None,
        batch_size: int = 100,
    ) -> list[list[float]]:
        """Batches large text lists so callers don't hand-roll chunking loops."""
        texts = list(texts)
        results: list[list[float]] = []
        for i in range(0, len(texts), max(1, batch_size)):
            batch = texts[i:i + batch_size]
            results.extend(await self.embed_batch(batch, model=model, dimensions=dimensions, task_type=task_type))
        return results
