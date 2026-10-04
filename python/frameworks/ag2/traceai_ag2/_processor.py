"""Span processor that normalizes AG2 1.x ``TelemetryMiddleware`` spans.

AG2 1.x (PyPI ``ag2``, import ``ag2``) already emits OpenTelemetry GenAI spans
from ``ag2.middleware.builtin.telemetry.TelemetryMiddleware``. This processor
does not create spans. On every span from AG2's instrumentation scope it only:

* sets ``gen_ai.span.kind`` from ``gen_ai.operation.name`` for the operation
  strings AG2 emits that the Microsoft Agent Framework table also maps
  (``chat``, ``execute_tool``, ``invoke_agent``);
* copies three non-semconv usage keys onto their dotted GenAI names, keeping
  the originals;
* applies ``TraceConfig`` as a second content gate (``hide_inputs`` /
  ``hide_outputs`` and the other ``TraceConfig.mask`` rules).

Spans from any other instrumentation scope pass through untouched.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from fi_instrumentation.instrumentation.config import TraceConfig
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

logger = logging.getLogger(__name__)

# ``ag2/_telemetry_consts.py`` line 42 at ag2 1.1.2 (same value since 1.0.0):
# the scope every AG2 tracer (agent middleware and network hub) reports.
AG2_INSTRUMENTATION_SCOPE = "opentelemetry.instrumentation.ag2"

# Operation strings read from ``ag2/middleware/builtin/telemetry.py`` at 1.1.2:
#   "invoke_agent"       line 355 (span "invoke_agent {agent_name}")
#   "chat"               line 467 (span "chat" / "chat {model}")
#   "execute_tool"       line 535 (span "execute_tool {tool_name}")
#   "await_human_input"  line 574 (span "await_human_input {agent_name}")
# The ``record_usage {kind}`` span (line 420) sets no operation name.
# Only strings that the Microsoft Agent Framework table also maps get a kind.
# ``await_human_input`` is not in that table, so it is left without a kind
# rather than guessed.
OPERATION_TO_SPAN_KIND: Mapping[str, str] = {
    "chat": FiSpanKindValues.LLM.value,
    "execute_tool": FiSpanKindValues.TOOL.value,
    "invoke_agent": FiSpanKindValues.AGENT.value,
}

# AG2 spelling -> GenAI semconv spelling. AG2 sets the left-hand keys at
# telemetry.py lines 441-446 (record_usage span) and 505-510 (chat span) at
# 1.1.2, only when the usage field is non-zero. Originals are kept.
USAGE_KEY_ALIASES: Mapping[str, str] = {
    "gen_ai.usage.cache_creation_input_tokens": "gen_ai.usage.cache_creation.input_tokens",
    "gen_ai.usage.cache_read_input_tokens": "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.thinking_tokens": "gen_ai.usage.reasoning.output_tokens",
}

# Content keys ``TraceConfig.mask`` does not know about. AG2 sets them only
# when ``capture_content=True`` (telemetry.py lines 541, 555, 578, 588).
_TOOL_ARGUMENTS = SpanAttributes.GEN_AI_TOOL_CALL_ARGUMENTS  # gen_ai.tool.call.arguments
_TOOL_RESULT = SpanAttributes.GEN_AI_TOOL_CALL_RESULT  # gen_ai.tool.call.result
_HUMAN_INPUT_PROMPT = "ag2.human_input.prompt"
_HUMAN_INPUT_RESPONSE = "ag2.human_input.response"
_INPUT_CONTENT_KEYS = (_TOOL_ARGUMENTS, _HUMAN_INPUT_PROMPT)
_OUTPUT_CONTENT_KEYS = (_TOOL_RESULT, _HUMAN_INPUT_RESPONSE)


def span_kind_for(attributes: Mapping[str, Any]) -> Optional[str]:
    """Return the Future AGI span kind for an AG2 span, or ``None``."""
    operation = attributes.get(SpanAttributes.GEN_AI_OPERATION_NAME)
    if not isinstance(operation, str):
        return None
    return OPERATION_TO_SPAN_KIND.get(operation)


def normalize_attributes(
    attributes: Mapping[str, Any],
    config: Optional[TraceConfig] = None,
) -> Dict[str, Any]:
    """Return a new attribute dict with the kind, aliases and content gate applied.

    Never removes a key AG2 set, except where ``config`` asks to hide content
    and where a duplicate usage span's promoted token keys move under
    ``ag2.usage.*`` (see ``_demote_duplicate_usage``).
    """
    mapped: Dict[str, Any] = dict(attributes)

    if SpanAttributes.GEN_AI_SPAN_KIND not in mapped:
        kind = span_kind_for(mapped)
        if kind is not None:
            mapped[SpanAttributes.GEN_AI_SPAN_KIND] = kind

    for source, target in USAGE_KEY_ALIASES.items():
        if source in mapped and target not in mapped:
            mapped[target] = mapped[source]

    _demote_duplicate_usage(mapped)

    if config is not None:
        mapped = _apply_trace_config(mapped, config)
    return mapped


# fi-collector promotes these keys into the token columns on any span
# (fi-collector/pkg/adapter/adapter.go inputTokenKeys/outputTokenKeys/
# totalTokenKeys), and Observe sums total_tokens over every span of a trace.
_PROMOTED_TOKEN_KEYS = (
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.total_tokens",
)
# ``record_usage model_call`` repeats the chat span's tokens (telemetry.py
# 437-440 vs 501-504 at 1.1.2). ``aggregation`` is a sum by definition.
# ``subtask`` and ``compaction`` are left alone: no chat span repeats them.
_DUPLICATE_USAGE_KINDS = frozenset({"model_call", "aggregation"})


def _demote_duplicate_usage(mapped: Dict[str, Any]) -> None:
    """Move a duplicate usage span's promoted token keys under ``ag2.usage.*``.

    The values are kept, so nothing AG2 reported is lost, but the trace total
    counts each model call once.
    """
    if mapped.get("ag2.span.type") != "usage":
        return
    if mapped.get("ag2.usage.kind") not in _DUPLICATE_USAGE_KINDS:
        return
    for key in _PROMOTED_TOKEN_KEYS:
        if key in mapped:
            value = mapped.pop(key)
            mapped.setdefault("ag2.usage." + key.rsplit(".", 1)[1], value)


def _apply_trace_config(attributes: Dict[str, Any], config: TraceConfig) -> Dict[str, Any]:
    hidden = set()
    if config.hide_inputs:
        hidden.update(_INPUT_CONTENT_KEYS)
    if config.hide_outputs:
        hidden.update(_OUTPUT_CONTENT_KEYS)

    masked: Dict[str, Any] = {}
    for key, value in attributes.items():
        if key in hidden:
            continue
        value = config.mask(key, value)
        if value is None:
            continue
        masked[key] = value
    return masked


class AG2SpanProcessor(SpanProcessor):
    """Normalize AG2 ``TelemetryMiddleware`` spans before export.

    Install it ahead of the exporting processor (``install_span_processor``
    does this) so the exporter sees the added keys.
    """

    def __init__(self, config: Optional[TraceConfig] = None) -> None:
        self._config = config if config is not None else TraceConfig()
        self._shutdown = False

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        return

    def on_end(self, span: ReadableSpan) -> None:
        if self._shutdown:
            return
        scope = getattr(getattr(span, "instrumentation_scope", None), "name", None)
        if scope != AG2_INSTRUMENTATION_SCOPE:
            return
        try:
            current = dict(span.attributes or {})
            mapped = normalize_attributes(current, self._config)
            if mapped != current:
                # ``ReadableSpan.attributes`` is a read-only view over
                # ``_attributes``; every later processor and exporter in the
                # chain receives this same object.
                span._attributes = mapped  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive
            logger.debug("traceai-ag2: could not normalize span %r", getattr(span, "name", None), exc_info=True)

    def shutdown(self) -> None:
        self._shutdown = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
