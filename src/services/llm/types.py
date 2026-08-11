"""Provider-agnostic data model for LLM interactions. No provider SDK symbols here —
adapters in providers/ translate to/from these types."""
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Union


class Role(str, Enum):
    USER = "user"
    MODEL = "model"


class SafetyPolicy(str, Enum):
    DEFAULT = "default"
    PERMISSIVE = "permissive"


class Tool(str, Enum):
    WEB_SEARCH = "web_search"


@dataclass(frozen=True)
class Part:
    text: str | None = None
    data: bytes | None = None
    mime_type: str | None = None

    @classmethod
    def from_text(cls, text: str) -> "Part":
        return cls(text=text)

    @classmethod
    def from_bytes(cls, data: bytes, mime_type: str) -> "Part":
        return cls(data=data, mime_type=mime_type)


@dataclass
class Message:
    role: Role
    parts: list[Part]

    @staticmethod
    def _as_parts(items: tuple[Union[str, Part], ...]) -> list[Part]:
        return [Part.from_text(item) if isinstance(item, str) else item for item in items]

    @classmethod
    def user(cls, *items: Union[str, Part]) -> "Message":
        return cls(role=Role.USER, parts=cls._as_parts(items))

    @classmethod
    def model(cls, *items: Union[str, Part]) -> "Message":
        return cls(role=Role.MODEL, parts=cls._as_parts(items))


@dataclass
class CacheHandle:
    """Either a provider-native cache reference (`name`), or the parts to inline on every
    call because caching is unavailable or unsupported (`parts`)."""
    name: str | None
    parts: list[Part] = field(default_factory=list)


@dataclass
class GenerationConfig:
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_output_tokens: int | None = None
    seed: int | None = None
    system_instruction: str | None = None
    json_output: bool = False
    response_schema: Any = None  # a pydantic BaseModel subclass, or a raw JSON-schema dict
    safety: SafetyPolicy = SafetyPolicy.DEFAULT
    thinking_budget: int | None = None  # None=provider default, -1=dynamic, 0=off, N=fixed budget
    include_thoughts: bool = False
    tools: list[Tool] = field(default_factory=list)
    cache: CacheHandle | None = None
    provider_options: dict[str, dict] = field(default_factory=dict)  # escape hatch, keyed by provider name

    def copy(self, **overrides) -> "GenerationConfig":
        return replace(self, **overrides)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    text: str
    usage: Usage | None
    model: str
    provider: str
    raw: Any = None
