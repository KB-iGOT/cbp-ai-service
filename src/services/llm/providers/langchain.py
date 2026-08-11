"""Generic multi-provider adapter via LangChain — the escape hatch for providers beyond
Gemini (OpenAI, Anthropic, Bedrock, ...) without touching any call site. Optional
dependency (see the `langchain` extra in pyproject.toml); this module is only imported
when LLM_PROVIDER=langchain or LLM_EMBEDDING_PROVIDER=langchain is selected — see
registry.py, which defers the import until then.

The raw dict schemas used at several call sites in this codebase (e.g. DOMAIN_FROM_WAO_SCHEMA)
use Gemini's native uppercase-type schema format ("OBJECT", "ARRAY", "STRING", ...), which is
not a standard JSON Schema and lacks the top-level "title" that with_structured_output's
OpenAI-function-calling conversion requires as the synthesized tool name.
_normalize_schema_dict() converts those to a standard schema so they work through this adapter
too, without touching the call sites that built them for Gemini."""
import base64
import json
import os
import time
from typing import Any, Sequence

from pydantic import BaseModel

from ....core.configs import settings
from ....core.logger import logger
from ..base import Capability, EmbeddingProvider, LLMProvider
from ..types import GenerationConfig, LLMResponse, Message, Part, Role, Usage

try:
    from langchain.chat_models import init_chat_model
except ImportError as e:
    raise ImportError(
        "LangChainProvider requires the 'langchain' extra: install with "
        "`uv sync --extra langchain` (or `pip install cbp-ai-service[langchain]`)."
    ) from e


def _ensure_vertex_credentials() -> None:
    """The `google_vertexai:` provider prefix (chat and embeddings) resolves credentials via
    ADC, which reads this env var — GeminiProvider sets it too, but that constructor never
    runs when LLM_PROVIDER=langchain, so each LangChain-side adapter sets it itself."""
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS


_GEMINI_TYPE_TO_JSON_SCHEMA_TYPE = {
    "OBJECT": "object",
    "ARRAY": "array",
    "STRING": "string",
    "INTEGER": "integer",
    "NUMBER": "number",
    "BOOLEAN": "boolean",
}

# Key used to wrap a non-object-root schema (see _normalize_schema_dict) — chosen to be
# distinctive enough that it won't collide with a real property name in this codebase's schemas.
_WRAPPED_RESULT_KEY = "_wrapped_result"


def _normalize_schema_dict(schema: dict, *, title: str = "StructuredOutput") -> tuple[dict, bool]:
    """Recursively lowercase Gemini's native uppercase schema `type` values, ensure a
    top-level `title` (required as the synthesized tool name), and — if the schema's root
    isn't an object — wrap it in one. Vertex AI's live function-calling API rejects a
    non-OBJECT root ("functionDeclaration parameters schema should be of type OBJECT"), even
    though with_structured_output builds the Runnable for it without complaint; several raw
    dict schemas in this codebase are ARRAY-rooted (e.g. the course-filtering response and
    DOMAIN_FROM_WAO_SCHEMA), since Gemini's native response_schema has no such restriction.

    Returns (normalized_schema, wrapped) — callers must unwrap `_WRAPPED_RESULT_KEY` from the
    parsed result when `wrapped` is True."""
    def _convert(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        converted: dict = {}
        for key, value in node.items():
            if key == "type" and isinstance(value, str):
                converted[key] = _GEMINI_TYPE_TO_JSON_SCHEMA_TYPE.get(value, value.lower())
            elif key == "properties" and isinstance(value, dict):
                converted[key] = {name: _convert(prop) for name, prop in value.items()}
            elif key == "items":
                converted[key] = _convert(value)
            else:
                converted[key] = value
        return converted

    converted = _convert(schema)
    wrapped = converted.get("type") != "object"
    if wrapped:
        converted = {
            "type": "object",
            "properties": {_WRAPPED_RESULT_KEY: converted},
            "required": [_WRAPPED_RESULT_KEY],
        }
    converted.setdefault("title", title)
    return converted, wrapped


def _resolve_model(model: str) -> tuple[str, dict]:
    """init_chat_model expects "<provider>:<model>". Every call site in this codebase
    passes a bare Gemini model name (e.g. "gemini-2.5-pro") — default those to
    google_vertexai, the provider this codebase already runs Gemini through.

    When defaulting to google_vertexai, pass this app's own project/location explicitly
    rather than letting langchain-google-vertexai fall back to its own default (observed to
    be "us-central1", not this app's configured GOOOGLE_PROJECT_LOCATION_GLOBAL="global") —
    a model published under one Vertex AI location is not necessarily published under
    another, as gemini-embedding-2 (global-only, not us-central1) demonstrated live."""
    if ":" in model:
        return model, {}
    return f"google_vertexai:{model}", {
        "project": settings.GOOGLE_PROJECT_ID,
        "location": settings.GOOOGLE_PROJECT_LOCATION_GLOBAL,
    }


def _to_langchain_content_block(part: Part) -> dict:
    if part.data is not None:
        return {
            "type": "media",
            "mime_type": part.mime_type or "application/octet-stream",
            "data": base64.b64encode(part.data).decode("ascii"),
        }
    return {"type": "text", "text": part.text or ""}


def _to_langchain_messages(contents: str | Sequence[Message], system_instruction: str | None):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    messages = [SystemMessage(content=system_instruction)] if system_instruction else []
    if isinstance(contents, str):
        messages.append(HumanMessage(content=contents))
        return messages
    for message in contents:
        content = [_to_langchain_content_block(p) for p in message.parts]
        cls = AIMessage if message.role == Role.MODEL else HumanMessage
        messages.append(cls(content=content))
    return messages


def _extract_usage(response: Any) -> Usage:
    meta = getattr(response, "usage_metadata", None) if response is not None else None
    if not meta:
        return Usage()
    return Usage(
        input_tokens=meta.get("input_tokens", 0) or 0,
        output_tokens=meta.get("output_tokens", 0) or 0,
        total_tokens=meta.get("total_tokens", 0) or 0,
    )


class LangChainProvider(LLMProvider):
    name = "langchain"

    def __init__(self):
        _ensure_vertex_credentials()
        self._models: dict[str, Any] = {}
        logger.info("LangChainProvider initialized")

    def _get_chat_model(self, model: str, config: GenerationConfig):
        key = f"{model}:{config.temperature}:{config.top_p}:{config.max_output_tokens}"
        chat = self._models.get(key)
        if chat is None:
            kwargs: dict = {}
            if config.temperature is not None:
                kwargs["temperature"] = config.temperature
            if config.top_p is not None:
                kwargs["top_p"] = config.top_p
            if config.max_output_tokens is not None:
                kwargs["max_tokens"] = config.max_output_tokens
            model_id, provider_kwargs = _resolve_model(model)
            chat = init_chat_model(model_id, **kwargs, **provider_kwargs)
            self._models[key] = chat
        return chat

    def supports(self, capability: Capability) -> bool:
        return capability in {Capability.STREAMING, Capability.NATIVE_JSON_SCHEMA, Capability.NATIVE_FILE_INPUT}

    async def generate(
        self,
        contents: str | Sequence[Message],
        *,
        model: str,
        config: GenerationConfig | None = None,
    ) -> LLMResponse:
        config = config or GenerationConfig()
        chat = self._get_chat_model(model, config)
        messages = _to_langchain_messages(contents, config.system_instruction)

        start = time.monotonic()
        if config.response_schema is not None:
            schema = config.response_schema
            wrapped = False
            if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
                schema, wrapped = _normalize_schema_dict(schema)
            structured = chat.with_structured_output(schema, include_raw=True)
            result = await structured.ainvoke(messages)
            latency_ms = (time.monotonic() - start) * 1000
            parsed, raw = result.get("parsed"), result.get("raw")
            if wrapped and isinstance(parsed, dict) and _WRAPPED_RESULT_KEY in parsed:
                parsed = parsed[_WRAPPED_RESULT_KEY]
            if isinstance(parsed, BaseModel):
                text = parsed.model_dump_json()
            elif parsed is not None:
                text = json.dumps(parsed)
            else:
                text = getattr(raw, "content", "") or ""
            usage = _extract_usage(raw)
            self._record_usage(model=model, usage=usage, latency_ms=latency_ms)
            return LLMResponse(text=text, usage=usage, model=model, provider=self.name, raw=raw)

        response = await chat.ainvoke(messages)
        latency_ms = (time.monotonic() - start) * 1000
        usage = _extract_usage(response)
        self._record_usage(model=model, usage=usage, latency_ms=latency_ms)
        return LLMResponse(text=response.content or "", usage=usage, model=model, provider=self.name, raw=response)

    async def stream(
        self,
        contents: str | Sequence[Message],
        *,
        model: str,
        config: GenerationConfig | None = None,
    ):
        config = config or GenerationConfig()
        chat = self._get_chat_model(model, config)
        messages = _to_langchain_messages(contents, config.system_instruction)
        async for chunk in chat.astream(messages):
            if chunk.content:
                yield chunk.content


class LangChainEmbeddingProvider(EmbeddingProvider):
    name = "langchain"

    def __init__(self):
        _ensure_vertex_credentials()
        self._embedders: dict[str, Any] = {}
        logger.info("LangChainEmbeddingProvider initialized")

    def _get_embedder(self, model: str, dimensions: int | None):
        # VertexAIEmbeddings takes `dimensions` at construction, not per call — the cache key
        # must include it, or a second caller requesting a different width (e.g. 768 for
        # designation matching vs 1536 for course search) would silently get back an embedder
        # built for the first width instead.
        key = (model, dimensions)
        embedder = self._embedders.get(key)
        if embedder is None:
            from langchain_google_vertexai import VertexAIEmbeddings
            # Explicit project/location — same reasoning as _resolve_model in this module:
            # langchain-google-vertexai's own default location is not this app's configured
            # one, and a model published under one Vertex AI location is not necessarily
            # published under another (gemini-embedding-2 is global-only, not us-central1).
            embedder = VertexAIEmbeddings(
                model_name=model,
                project=settings.GOOGLE_PROJECT_ID,
                location=settings.GOOOGLE_PROJECT_LOCATION_GLOBAL,
                dimensions=dimensions,
            )
            self._embedders[key] = embedder
        return embedder

    async def embed_batch(
        self,
        texts: Sequence[str],
        *,
        model: str,
        dimensions: int | None = None,
        task_type: str | None = None,
    ) -> list[list[float]]:
        return await self._get_embedder(model, dimensions).aembed_documents(list(texts))
