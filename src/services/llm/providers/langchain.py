"""Generic multi-provider adapter via LangChain — the escape hatch for providers beyond
Gemini (OpenAI, Anthropic, Bedrock, ...) without touching any call site. Optional dependency
(see the `langchain` extra in pyproject.toml); this module is only imported when
LLM_PROVIDER=langchain or LLM_EMBEDDING_PROVIDER=langchain — see registry.py, which defers the
import until then.

Google is reached through `langchain-google-genai`, NOT `langchain-google-vertexai`: the
latter's ChatVertexAI / VertexAIEmbeddings are deprecated as of LangChain 3.2 and slated for
removal in 4.0. langchain-google-genai serves both the Gemini Developer API and Gemini on
Vertex AI (`vertexai=True` + project/location), and exposes `output_dimensionality` on
embeddings, so it covers everything this codebase needs from the Google side.

Model names arriving here may carry a LangChain "provider:model" prefix (e.g. "openai:gpt-5"),
courtesy of LLM_MODEL_MAP — see ../models.py. A bare name defaults to Google, since every
call site in this codebase passes a bare Gemini model name.
"""
import base64
import json
import os
import time
from typing import Any, Sequence

from pydantic import BaseModel

from ....core.configs import settings
from ....core.logger import logger
from ..base import Capability, EmbeddingProvider, LLMProvider
from ..models import resolve_model, split_provider
from ..types import GenerationConfig, LLMResponse, Message, Part, Role, Usage

try:
    from langchain.chat_models import init_chat_model
    from langchain.embeddings import init_embeddings
except ImportError as e:
    raise ImportError(
        "LangChainProvider requires the 'langchain' extra: install with "
        "`uv sync --extra langchain` (or `pip install cbp-ai-service[langchain]`)."
    ) from e

# Provider used when a model name carries no "provider:" prefix. Bare names in this codebase
# are always Gemini models.
_DEFAULT_PROVIDER = "google_genai"

# Providers whose credentials are Google ADC (a service-account JSON path), rather than an
# API key in the environment.
_GOOGLE_PROVIDERS = {"google_genai", "google_vertexai"}

# provider prefix -> (settings attribute holding the key, env var the integration reads)
_API_KEY_ENV = {
    "openai": ("OPENAI_API_KEY", "OPENAI_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
}

_GEMINI_TYPE_TO_JSON_SCHEMA_TYPE = {
    "OBJECT": "object",
    "ARRAY": "array",
    "STRING": "string",
    "INTEGER": "integer",
    "NUMBER": "number",
    "BOOLEAN": "boolean",
}

# Key used to wrap a non-object-root schema (see _normalize_schema_dict) — distinctive enough
# that it will not collide with a real property name in this codebase's schemas.
_WRAPPED_RESULT_KEY = "_wrapped_result"


def _ensure_credentials(provider: str) -> None:
    """Export whatever credential the target integration reads from the environment.

    GeminiProvider does this for Google too, but that constructor never runs when
    LLM_PROVIDER=langchain, so this adapter must set it itself.
    """
    if provider in _GOOGLE_PROVIDERS:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS
        return
    entry = _API_KEY_ENV.get(provider)
    if not entry:
        # An unrecognised provider is not necessarily wrong (LangChain supports many more);
        # it just means we have no key wiring for it, so it must come from the ambient env.
        logger.info(
            f"No credential wiring for LangChain provider '{provider}'; relying on the "
            "ambient environment to supply it."
        )
        return
    settings_attr, env_var = entry
    key = getattr(settings, settings_attr, "") or ""
    if key:
        os.environ[env_var] = key
    elif not os.environ.get(env_var):
        logger.warning(
            f"Provider '{provider}' selected but neither {settings_attr} (.env) nor {env_var} "
            "(environment) is set — calls will fail authentication."
        )


def _normalize_schema_dict(schema: dict, *, title: str = "StructuredOutput") -> tuple[dict, bool]:
    """Convert a Gemini-native raw dict schema into standard JSON Schema for providers that
    route structured output through OpenAI-style function calling.

    Two transformations, both required by that path and both harmless to a real JSON Schema:
      * lowercase Gemini's uppercase `type` values ("OBJECT" -> "object", ...)
      * wrap a non-object root in a single-property object, because function-call parameters
        must be an object; several schemas in this codebase are ARRAY-rooted (the
        course-filtering response, DOMAIN_FROM_WAO_SCHEMA), which Gemini's native
        response_schema permits but function calling does not.
    A top-level `title` is also added, since it becomes the synthesized tool name.

    Returns (schema, wrapped); when `wrapped` is True the caller must unwrap
    `_WRAPPED_RESULT_KEY` from the parsed result.
    """
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


def _resolve_target(model: str) -> tuple[str, str, dict]:
    """Map a caller-supplied model name to (provider, langchain_model_id, provider_kwargs).

    Google gets project/location passed explicitly rather than relying on the integration's
    own default (observed to be us-central1, not this app's configured
    GOOOGLE_PROJECT_LOCATION_GLOBAL="global") — a model published in one Vertex AI location is
    not necessarily published in another, as gemini-embedding-2 (global-only) demonstrated
    live. Those kwargs are Google-specific and must not leak to other providers, which would
    reject them as unexpected arguments.
    """
    resolved = resolve_model(model)
    provider, bare = split_provider(resolved)
    provider = provider or _DEFAULT_PROVIDER

    kwargs: dict = {}
    if provider in _GOOGLE_PROVIDERS:
        kwargs = {
            "project": settings.GOOGLE_PROJECT_ID,
            "location": settings.GOOOGLE_PROJECT_LOCATION_GLOBAL,
            "vertexai": settings.GOOGLE_GENAI_USE_VERTEXAI,
        }
    return provider, f"{provider}:{bare}", kwargs


def _to_langchain_content_block(part: Part, provider: str) -> dict:
    """Translate a neutral Part into the target provider's content-block shape.

    Binary attachments are the one genuinely non-portable piece of the message format: each
    vendor names and nests them differently, so there is no single block that works everywhere.
    """
    if part.data is None:
        return {"type": "text", "text": part.text or ""}

    mime = part.mime_type or "application/octet-stream"
    encoded = base64.b64encode(part.data).decode("ascii")

    if provider in _GOOGLE_PROVIDERS:
        return {"type": "media", "mime_type": mime, "data": encoded}
    if provider == "anthropic":
        block_type = "image" if mime.startswith("image/") else "document"
        return {
            "type": block_type,
            "source": {"type": "base64", "media_type": mime, "data": encoded},
        }
    if provider == "openai":
        if mime.startswith("image/"):
            return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
        return {"type": "file", "file": {"filename": "attachment", "file_data": f"data:{mime};base64,{encoded}"}}

    raise ValueError(
        f"LangChainProvider does not know how to attach '{mime}' content for provider "
        f"'{provider}'. Add a content-block mapping in _to_langchain_content_block, or route "
        f"this call to a provider that has one ({sorted(_GOOGLE_PROVIDERS | {'openai', 'anthropic'})})."
    )


def _to_langchain_messages(
    contents: str | Sequence[Message], system_instruction: str | None, provider: str
):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    messages = [SystemMessage(content=system_instruction)] if system_instruction else []
    if isinstance(contents, str):
        messages.append(HumanMessage(content=contents))
        return messages
    for message in contents:
        content = [_to_langchain_content_block(p, provider) for p in message.parts]
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
        self._models: dict[tuple, Any] = {}
        logger.info("LangChainProvider initialized")

    def _get_chat_model(self, model: str, config: GenerationConfig) -> tuple[Any, str, str]:
        """Returns (chat_model, provider, model_id). `model_id` is the fully-resolved
        "provider:model" that actually serves the request — report that rather than the name
        the caller asked for, so a LLM_MODEL_MAP redirect is visible in usage logs instead of
        logs claiming a model that never ran."""
        provider, model_id, provider_kwargs = _resolve_target(model)
        key = (model_id, config.temperature, config.top_p, config.max_output_tokens)
        chat = self._models.get(key)
        if chat is None:
            _ensure_credentials(provider)
            kwargs: dict = {}
            if config.temperature is not None:
                kwargs["temperature"] = config.temperature
            if config.top_p is not None:
                kwargs["top_p"] = config.top_p
            if config.max_output_tokens is not None:
                kwargs["max_tokens"] = config.max_output_tokens
            chat = init_chat_model(model_id, **kwargs, **provider_kwargs)
            self._models[key] = chat
        return chat, provider, model_id

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
        chat, provider, model_id = self._get_chat_model(model, config)
        messages = _to_langchain_messages(contents, config.system_instruction, provider)

        start = time.monotonic()
        if config.response_schema is not None:
            schema = config.response_schema
            wrapped = False
            is_pydantic = isinstance(schema, type) and issubclass(schema, BaseModel)
            if not is_pydantic:
                # langchain-google-genai forwards a raw dict straight to Gemini's native
                # response_schema, which accepts uppercase types and a non-object root — so
                # leave Google's schemas untouched and only normalize for the
                # function-calling-based providers that actually need it.
                if provider in _GOOGLE_PROVIDERS:
                    schema = dict(schema)
                    schema.setdefault("title", "StructuredOutput")
                else:
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
            self._record_usage(model=model_id, usage=usage, latency_ms=latency_ms)
            return LLMResponse(text=text, usage=usage, model=model_id, provider=self.name, raw=raw)

        response = await chat.ainvoke(messages)
        latency_ms = (time.monotonic() - start) * 1000
        usage = _extract_usage(response)
        self._record_usage(model=model_id, usage=usage, latency_ms=latency_ms)
        return LLMResponse(text=response.content or "", usage=usage, model=model_id, provider=self.name, raw=response)

    async def stream(
        self,
        contents: str | Sequence[Message],
        *,
        model: str,
        config: GenerationConfig | None = None,
    ):
        config = config or GenerationConfig()
        chat, provider, _ = self._get_chat_model(model, config)
        messages = _to_langchain_messages(contents, config.system_instruction, provider)
        async for chunk in chat.astream(messages):
            if chunk.content:
                yield chunk.content


class LangChainEmbeddingProvider(EmbeddingProvider):
    name = "langchain"

    def __init__(self):
        self._embedders: dict[tuple, Any] = {}
        logger.info("LangChainEmbeddingProvider initialized")

    def _get_embedder(self, model: str, dimensions: int | None):
        provider, model_id, provider_kwargs = _resolve_target(model)
        # Output width is fixed at construction, not per call, so it belongs in the cache key —
        # otherwise a caller wanting 768 would be handed an embedder built for 1536 (this app
        # legitimately uses both: 768 for designation matching, 1536 for course search).
        key = (model_id, dimensions)
        embedder = self._embedders.get(key)
        if embedder is None:
            _ensure_credentials(provider)
            kwargs = dict(provider_kwargs)
            if dimensions:
                # langchain-google-genai names this output_dimensionality (matching the native
                # SDK); OpenAI's integration calls it dimensions.
                kwargs["output_dimensionality" if provider in _GOOGLE_PROVIDERS else "dimensions"] = dimensions
            embedder = init_embeddings(model_id, **kwargs)
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
