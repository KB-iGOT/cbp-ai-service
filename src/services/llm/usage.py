"""Per-call usage/latency observability, common to every provider. Token usage was
previously unlogged (see the commented-out line in the old role_mapping_service); every
adapter now emits one structured record per call, and callers may additionally register
their own callback (e.g. to replace the SDK monkey-patching used in bulk_scripts for
token accounting)."""
from dataclasses import dataclass
from typing import Callable

from ...core.logger import logger


@dataclass
class UsageRecord:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    label: str | None = None


_callbacks: list[Callable[[UsageRecord], None]] = []


def register_usage_callback(fn: Callable[[UsageRecord], None]) -> None:
    _callbacks.append(fn)


def emit_usage(record: UsageRecord) -> None:
    logger.info(
        f"LLM usage: provider={record.provider} model={record.model} "
        f"input_tokens={record.input_tokens} output_tokens={record.output_tokens} "
        f"latency_ms={record.latency_ms:.0f}" + (f" label={record.label}" if record.label else "")
    )
    for callback in _callbacks:
        try:
            callback(record)
        except Exception:
            logger.exception("LLM usage callback failed")
