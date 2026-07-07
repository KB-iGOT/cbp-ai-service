"""Langfuse observability for LLM calls (opt-in, zero-cost when disabled).

Toggle with LANGFUSE_ENABLED=true + LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY in .env.
When disabled (default), every function here is a no-op — Langfuse is never imported
and nothing can break the request/generation flow.

Model for this codebase (not chat sessions):
  - `trace(...)`      opens ONE root span for a business operation (a role-mapping run,
                      a course-recommendation, etc.), tagged with user_id + a logical
                      session_id (e.g. "<state_center_id>:<department_id>") for filtering.
  - `generation(...)` opens a child GENERATION span per individual LLM call.
  - `record_gemini_usage(resp)` attaches model + token counts (input / output / thinking /
                      cached / total) to the current generation span.

Everything is wrapped in try/except so a Langfuse outage never affects the app.
"""
from __future__ import annotations

import contextvars
import logging
import sys
from contextlib import contextmanager, nullcontext
from typing import Any, Optional

logger = logging.getLogger("ai_cbp_service")

_client: Any = None
_enabled: bool = False
_MAX_IO_CHARS = 4000  # cap input/output payloads sent to Langfuse
# True while inside a manual generation() span, so the auto-instrument patch skips
# (avoids double spans for calls we already wrap by hand).
_manual_gen: contextvars.ContextVar[bool] = contextvars.ContextVar("lf_manual_gen", default=False)
# Request/flow-scoped identity (user_id, session_id, tags) applied to every trace + auto-span,
# so LLM calls that aren't individually wrapped still carry who/where. Set via set_identity().
_identity: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("lf_identity", default=None)


def set_identity(*, user_id: Optional[str] = None, session_id: Optional[str] = None,
                 tags: Optional[list] = None) -> None:
    """Attach user_id / session_id / tags to all LLM calls in the current context (one HTTP
    request or task). Call once at the top of a handler/task. No-op when tracing is disabled.
    NOTE: contextvars don't cross into FastAPI BackgroundTasks — for those, pass identity
    explicitly to the task (role-mapping/course-rec already do)."""
    if not _enabled:
        return
    _identity.set({
        "user_id": str(user_id) if user_id else None,
        "session_id": str(session_id) if session_id else None,
        "tags": tags or [],
    })


# ── lifecycle ────────────────────────────────────────────────────────────────
def init() -> None:
    """Initialise once at FastAPI startup (inside lifespan)."""
    global _client, _enabled
    try:
        from ..core.configs import settings
    except Exception:
        from src.core.configs import settings  # fallback for standalone scripts

    if not getattr(settings, "LANGFUSE_ENABLED", False):
        logger.info("[tracing] Langfuse disabled")
        return
    if not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
        logger.warning("[tracing] LANGFUSE_ENABLED=true but keys missing — tracing disabled")
        return
    try:
        from langfuse import Langfuse
        host = settings.LANGFUSE_HOST or "https://cloud.langfuse.com"
        environment = (getattr(settings, "LANGFUSE_ENVIRONMENT", "") or
                       str(getattr(settings.ENVIRONMENT, "value", settings.ENVIRONMENT)))
        _client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=host,
            sample_rate=getattr(settings, "LANGFUSE_SAMPLE_RATE", 1.0),
            environment=environment,
        )
        _enabled = True
        logger.info(f"[tracing] Langfuse enabled (host={host}, environment={environment})")
        _instrument_genai()   # auto-trace EVERY generate_content / embed_content call (all v1/v2/v3)
    except Exception as exc:
        logger.error(f"[tracing] Failed to init Langfuse — tracing disabled: {exc}")


def shutdown() -> None:
    """Flush pending spans at FastAPI shutdown."""
    if _enabled and _client is not None:
        try:
            _client.flush()
        except Exception as exc:
            logger.warning(f"[tracing] flush error: {exc}")


def is_enabled() -> bool:
    return _enabled


def _trunc(v: Any) -> Any:
    if isinstance(v, str) and len(v) > _MAX_IO_CHARS:
        return v[:_MAX_IO_CHARS] + f"… [+{len(v) - _MAX_IO_CHARS} chars]"
    return v


def _contents_to_text(contents: Any) -> Optional[str]:
    """Best-effort human-readable rendering of a genai `contents` argument (the prompt/input),
    for logging. Binary parts (PDFs, inline data) are noted, not dumped."""
    try:
        if contents is None:
            return None
        if isinstance(contents, str):
            return contents
        out = []
        items = contents if isinstance(contents, (list, tuple)) else [contents]
        for it in items:
            if isinstance(it, str):
                out.append(it); continue
            parts = getattr(it, "parts", None) or ([it] if getattr(it, "text", None) else [])
            for p in parts:
                t = getattr(p, "text", None)
                if t:
                    out.append(t)
                elif getattr(p, "inline_data", None) or getattr(p, "file_data", None):
                    out.append("[binary/pdf]")
        return "\n".join(out) if out else None
    except Exception:
        return None


# Friendly span labels per calling function — one central place; unmapped callers fall back
# to their raw function name. Keeps every LLM span human-readable without per-call-site code.
_OP_NAMES = {
    # role mapping (v2/v3)
    "_extract_designations": "rolemap/pass1-designations",
    "_generate_frac_for_batch": "rolemap/pass2-frac",
    "_generate_domain_from_wao": "rolemap/pass3-domain-from-wao",
    # course recommendation
    "generate_contextual_queries": "reco/contextual-queries",
    "infer_designation_group": "reco/designation-group",
    "get_filtered_courses_by_llm": "reco/course-filter",
    "get_general_courses_from_gemini": "reco/general-courses",
    "get_embedding": "reco/embedding",
    # document / meta summaries
    "generate_work_allocation_summary": "summary/work-allocation",
    "generate_acbp_plan_summary": "summary/acbp-plan",
    "_run_meta_summary": "summary/meta",
    # add-designation
    "generate_role_and_competencies": "add-designation/frac",
}


def _caller_operation() -> str:
    """Name the LLM span after the app function that made the call (skip genai + tracing frames),
    mapped to a friendly label via _OP_NAMES (raw function name as fallback)."""
    try:
        f = sys._getframe(2)
        for _ in range(12):
            if f is None:
                break
            fn = f.f_code.co_filename
            if "google/genai" not in fn and not fn.endswith("tracing.py"):
                name = f.f_code.co_name
                return _OP_NAMES.get(name, name)
            f = f.f_back
    except Exception:
        pass
    return "generate"


# ── operation-level trace ────────────────────────────────────────────────────
@contextmanager
def trace(*, name: str, user_id: Optional[str] = None, session_id: Optional[str] = None,
          tags: Optional[list] = None, **metadata: Any):
    """Root span for one business operation. Child generation() spans nest under it.
    `session_id` is any logical grouping key (dept scope, request id, …) for filtering."""
    if not _enabled or _client is None:
        yield
        return
    ident = _identity.get() or {}
    user_id = user_id or ident.get("user_id")
    session_id = session_id or ident.get("session_id")
    tags = tags or ident.get("tags") or []
    meta = {k: str(v) for k, v in metadata.items() if v is not None}
    started = False
    try:
        from langfuse import propagate_attributes
        with _client.start_as_current_observation(name=name, as_type="span", metadata=meta):
            with propagate_attributes(user_id=user_id, session_id=session_id,
                                      tags=tags or [], metadata=meta):
                started = True
                yield
    except Exception as exc:
        if started:
            raise
        logger.warning(f"[tracing] trace('{name}') setup error: {exc}")
        yield


# ── per-LLM-call generation span ─────────────────────────────────────────────
@contextmanager
def generation(*, model: str, operation: str, input: Any = None, **metadata: Any):
    """Child GENERATION span for a single LLM call. Call record_gemini_usage()/
    update_generation() inside to attach output + token counts."""
    if not _enabled or _client is None:
        yield
        return
    started = False
    try:
        meta = {"operation": operation, **{k: str(v) for k, v in metadata.items() if v is not None}}
        with _client.start_as_current_observation(
            name=f"llm:{operation}", as_type="generation", model=model,
            input=_trunc(input) if input is not None else None, metadata=meta):
            started = True
            _tok = _manual_gen.set(True)   # suppress the auto-patch for this call
            try:
                yield
            finally:
                _manual_gen.reset(_tok)
    except Exception as exc:
        if started:
            raise
        logger.warning(f"[tracing] generation('{operation}') setup error: {exc}")
        yield


def update_generation(*, output: Any = None, usage_details: Optional[dict] = None) -> None:
    if not _enabled or _client is None:
        return
    try:
        kwargs: dict[str, Any] = {}
        if output is not None:
            kwargs["output"] = _trunc(output)
        if usage_details:
            kwargs["usage_details"] = usage_details
        if kwargs:
            _client.update_current_generation(**kwargs)
    except Exception as exc:
        logger.debug(f"[tracing] update_generation failed: {exc}")


def record_gemini_usage(response: Any, *, output: Any = None) -> None:
    """Extract token counts from a google-genai response.usage_metadata and attach them
    (input / output / thinking / cached / total) to the current generation span."""
    if not _enabled or _client is None:
        return
    usage = None
    try:
        um = getattr(response, "usage_metadata", None)
        if um is not None:
            def g(attr):
                v = getattr(um, attr, None)
                return int(v) if isinstance(v, int) else 0
            usage = {
                "input": g("prompt_token_count"),
                "output": g("candidates_token_count"),
                "thinking": g("thoughts_token_count"),
                "cached": g("cached_content_token_count"),
                "total": g("total_token_count"),
            }
            usage = {k: v for k, v in usage.items() if v}  # drop zeros
        if output is None:
            output = getattr(response, "text", None)
    except Exception as exc:
        logger.debug(f"[tracing] record_gemini_usage parse failed: {exc}")
    update_generation(output=output, usage_details=usage)


# ── auto-instrumentation: trace EVERY genai call, everywhere ──────────────────
def _instrument_genai() -> None:
    """Monkeypatch google-genai's async generate_content + embed_content so every LLM call
    across the whole codebase (v1/v2/v3, services, any client instance) becomes a generation
    span with model + token counts — no per-call-site edits needed. Calls already wrapped by a
    manual generation() are skipped (via _manual_gen) so they aren't double-counted. Calls made
    outside any trace() get their own root trace; those inside one nest under it."""
    try:
        from google.genai.models import AsyncModels
    except Exception as exc:
        logger.warning(f"[tracing] genai auto-instrument unavailable: {exc}")
        return
    if getattr(AsyncModels, "_lf_patched", False):
        return

    _orig_generate = AsyncModels.generate_content
    _orig_embed = AsyncModels.embed_content

    def _ident_cm():
        """propagate_attributes(user_id/session_id/tags) from the request-scoped identity,
        so even auto-traced standalone calls carry who/where. nullcontext if none set."""
        ident = _identity.get() or {}
        if ident.get("user_id") or ident.get("session_id") or ident.get("tags"):
            try:
                from langfuse import propagate_attributes
                return propagate_attributes(user_id=ident.get("user_id"),
                                            session_id=ident.get("session_id"),
                                            tags=ident.get("tags") or [])
            except Exception:
                return nullcontext()
        return nullcontext()

    async def _traced_generate(self, **kwargs):
        if not _enabled or _client is None or _manual_gen.get():
            return await _orig_generate(self, **kwargs)
        model = kwargs.get("model", "unknown")
        op = _caller_operation()
        inp = _trunc(_contents_to_text(kwargs.get("contents")))
        started = False
        try:
            with _ident_cm(), _client.start_as_current_observation(
                    name=f"llm:{op}", as_type="generation", model=model,
                    input=inp, metadata={"operation": op}):
                started = True
                resp = await _orig_generate(self, **kwargs)   # LLM errors propagate & are recorded
                try:
                    record_gemini_usage(resp)
                except Exception:
                    pass
                return resp
        except Exception:
            if started:
                raise
            return await _orig_generate(self, **kwargs)   # span setup failed — plain call

    async def _traced_embed(self, **kwargs):
        if not _enabled or _client is None or _manual_gen.get():
            return await _orig_embed(self, **kwargs)
        model = kwargs.get("model", "unknown")
        op = _caller_operation()
        inp = _trunc(_contents_to_text(kwargs.get("contents")))
        started = False
        try:
            with _ident_cm(), _client.start_as_current_observation(
                    name=f"embed:{op}", as_type="generation", model=model,
                    input=inp, metadata={"operation": op}):
                started = True
                resp = await _orig_embed(self, **kwargs)
                try:
                    record_gemini_usage(resp, output=None)   # embeddings: usage only, no vector dump
                except Exception:
                    pass
                return resp
        except Exception:
            if started:
                raise
            return await _orig_embed(self, **kwargs)

    AsyncModels.generate_content = _traced_generate
    AsyncModels.embed_content = _traced_embed
    AsyncModels._lf_patched = True
    logger.info("[tracing] google-genai auto-instrumented (generate_content + embed_content)")
