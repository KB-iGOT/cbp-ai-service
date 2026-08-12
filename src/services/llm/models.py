"""Model-name redirection, applied by every adapter before a name reaches a provider.

Why this exists: a provider switch should be a config change, but several call sites pass a
hardcoded Gemini model literal (e.g. `model="gemini-2.5-pro"` in pdf_service, meta_summary_routes,
v1/v2 role mapping) instead of reading a setting. Those calls cannot follow LLM_PROVIDER on their
own. LLM_MODEL_MAP redirects any model name — literal or setting-derived — at the boundary, so no
call site has to change.

A mapped value may carry a LangChain "provider:model" prefix (e.g. "openai:gpt-5"), which the
LangChain adapter routes on. With an empty map (the default) every name passes through unchanged,
so behaviour is byte-identical to having no redirection layer.
"""
from ...core.configs import settings
from ...core.logger import logger

_warned: set[str] = set()


def resolve_model(model: str) -> str:
    """Apply LLM_MODEL_MAP to `model`. Returns the mapped name, or `model` unchanged."""
    mapped = (settings.LLM_MODEL_MAP or {}).get(model)
    if not mapped or mapped == model:
        return model
    # Log once per distinct redirection — silent model substitution is the kind of thing that
    # makes "why is this answer different?" take an afternoon to track down.
    if model not in _warned:
        _warned.add(model)
        logger.info(f"LLM_MODEL_MAP: routing model '{model}' -> '{mapped}'")
    return mapped


# Recognised LangChain provider prefixes. An allowlist rather than a shape heuristic because
# colons appear inside model names too — OpenAI fine-tunes ("ft:gpt-4o:acme:id") and Bedrock
# ARNs would both be mis-split by a "looks like a token" rule, silently routing to a provider
# named "ft" or "arn". Add an entry here to use a provider LangChain supports but this list
# omits; an unrecognised prefix is treated as part of the model name and warned about.
_KNOWN_PROVIDERS = frozenset({
    "openai", "azure_openai", "anthropic", "google_genai", "google_vertexai", "google_anthropic_vertex",
    "bedrock", "bedrock_converse", "cohere", "fireworks", "together", "mistralai", "groq",
    "ollama", "huggingface", "deepseek", "xai", "perplexity", "nvidia", "ibm", "databricks",
})

_unknown_prefix_warned: set[str] = set()


def split_provider(model: str) -> tuple[str | None, str]:
    """Split a LangChain-style "provider:model" string into (provider, model).

    Returns (None, model) when there is no recognised provider prefix — including when the
    colon belongs to the model name itself.
    """
    if ":" not in model:
        return None, model
    prefix, rest = model.split(":", 1)
    if prefix in _KNOWN_PROVIDERS and rest:
        return prefix, rest
    if prefix not in _unknown_prefix_warned:
        _unknown_prefix_warned.add(prefix)
        logger.warning(
            f"Model '{model}' has an unrecognised provider prefix '{prefix}'; treating the whole "
            f"string as a model name. If '{prefix}' is a LangChain provider you intend to use, add "
            f"it to _KNOWN_PROVIDERS in src/services/llm/models.py."
        )
    return None, model
