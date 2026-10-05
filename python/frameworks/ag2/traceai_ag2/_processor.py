"""Span processor that normalizes AG2 1.x ``TelemetryMiddleware`` spans.

AG2 1.x (PyPI ``ag2``, import ``ag2``) already emits OpenTelemetry GenAI spans
from ``ag2.middleware.builtin.telemetry.TelemetryMiddleware``. This processor
does not create spans. On every span from AG2's instrumentation scope it only:

* sets ``gen_ai.span.kind`` from ``gen_ai.operation.name`` for the operation
  strings AG2 emits that the Microsoft Agent Framework table also maps
  (``chat``, ``execute_tool``, ``invoke_agent``);
* copies three non-semconv usage keys onto their dotted GenAI names, keeping
  the originals;
* on ``record_usage`` spans whose tokens a chat span in the same trace already
  carries (every ``model_call``; a ``subtask`` rollup whose sub-agent is itself
  instrumented in that trace), moves ``gen_ai.usage.input_tokens`` /
  ``output_tokens`` / ``total_tokens`` to ``ag2.usage.*`` so the trace total
  counts each model call once. ``aggregation`` and ``compaction`` usage keeps
  its promoted tokens: AG2 calls the model outside the middleware for those;
* applies ``TraceConfig`` as a second content gate (``hide_inputs`` /
  ``hide_outputs`` and the other ``TraceConfig.mask`` rules);
* in ``on_start``, copies traceAI context attributes (``using_session``,
  ``using_user``, ``using_metadata``, ``using_attributes``, ...) onto the span
  without overriding keys AG2 sets, except ``session.id``, where the context
  wins.

Spans from any other instrumentation scope pass through untouched.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import AbstractSet, Any, Dict, FrozenSet, Mapping, Optional, Set, Tuple

from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from fi_instrumentation.instrumentation.config import TraceConfig
from fi_instrumentation.instrumentation.context_attributes import get_attributes_from_context
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
    *,
    instrumented_agents: AbstractSet[str] = frozenset(),
) -> Dict[str, Any]:
    """Return a new attribute dict with the kind, aliases and content gate applied.

    Never removes a key AG2 set, except where ``config`` asks to hide content
    and where a duplicate usage span's promoted token keys move under
    ``ag2.usage.*`` (see ``_demote_duplicate_usage``). ``instrumented_agents``
    are the agent names already seen on ``invoke_agent`` spans of the same
    trace; :class:`AG2SpanProcessor` tracks them.
    """
    mapped: Dict[str, Any] = dict(attributes)

    if SpanAttributes.GEN_AI_SPAN_KIND not in mapped:
        kind = span_kind_for(mapped)
        if kind is not None:
            mapped[SpanAttributes.GEN_AI_SPAN_KIND] = kind

    for source, target in USAGE_KEY_ALIASES.items():
        if source in mapped and target not in mapped:
            mapped[target] = mapped[source]

    _demote_duplicate_usage(mapped, instrumented_agents)

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
# Usage kinds AG2 emits (``ag2/_telemetry_consts.py`` ATTR_USAGE_KIND at
# 1.1.2) and whether a chat span in the same trace already carries the tokens:
#
# * ``model_call``: yes. ``Agent`` emits it right after the LLM call that
#   ``on_llm_call`` wrapped in a chat span (agent.py 1585-1595; telemetry.py
#   437-440 vs 501-504). Always demoted.
# * ``aggregation``: no. ``ag2/aggregate.py`` 97-99 and 193-195 call the model
#   client directly on a throwaway ``Context(MemoryStream())`` that never goes
#   through ``on_llm_call``; the ``UsageEvent`` (aggregate.py 214-216) is the
#   only record of that spend. Never demoted.
# * ``compaction``: no, for the same reason (compact.py 189-205). Never demoted.
# * ``subtask``: a rollup of a sub-agent's calls (tools/subagents/run_task.py
#   63-93, ``label`` = the sub-agent's name). It repeats chat spans only when
#   that sub-agent is itself instrumented, which shows as an
#   ``invoke_agent {label}`` span that ended earlier in the same trace. Demoted
#   only then; otherwise the rollup is the only copy and keeps its tokens.
_ALWAYS_DUPLICATE_USAGE_KINDS = frozenset({"model_call"})
_SUBTASK_USAGE_KIND = "subtask"
_USAGE_LABEL = "ag2.usage.label"
_AGENT_NAME = "gen_ai.agent.name"
_SESSION_ID = SpanAttributes.SESSION_ID  # session.id


def _scope_name(span: Any) -> Optional[str]:
    return getattr(getattr(span, "instrumentation_scope", None), "name", None)


def _span_key(span: Any) -> Tuple[int, int]:
    ctx = span.context
    return (ctx.trace_id, ctx.span_id) if ctx is not None else (0, id(span))


def _demote_duplicate_usage(mapped: Dict[str, Any], instrumented_agents: AbstractSet[str] = frozenset()) -> None:
    """Move a duplicate usage span's promoted token keys under ``ag2.usage.*``.

    ``instrumented_agents`` are the agent names seen on ``invoke_agent`` spans
    of the same trace. The values are kept, so nothing AG2 reported is lost,
    but the trace total counts each model call once.
    """
    if mapped.get("ag2.span.type") != "usage":
        return
    kind = mapped.get("ag2.usage.kind")
    if kind in _ALWAYS_DUPLICATE_USAGE_KINDS:
        pass
    elif kind == _SUBTASK_USAGE_KIND and mapped.get(_USAGE_LABEL) in instrumented_agents:
        pass
    else:
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

    It remembers, per trace, the agent names of ``invoke_agent`` spans that
    have ended, so a ``record_usage subtask`` rollup for an instrumented
    sub-agent is not counted on top of that sub-agent's own chat spans. The
    map is bounded to the most recent ``max_tracked_traces`` traces.
    """

    def __init__(self, config: Optional[TraceConfig] = None, *, max_tracked_traces: int = 1024) -> None:
        self._config = config if config is not None else TraceConfig()
        self._shutdown = False
        self._max_tracked_traces = max(1, int(max_tracked_traces))
        self._agents_by_trace: "OrderedDict[int, Set[str]]" = OrderedDict()
        # Context session ids of AG2 spans that started but have not ended.
        self._max_pending_spans = 16 * self._max_tracked_traces
        self._session_by_span: "OrderedDict[Tuple[int, int], Any]" = OrderedDict()
        self._lock = threading.Lock()

    def _note_agent(self, trace_id: int, attributes: Mapping[str, Any]) -> None:
        if attributes.get(SpanAttributes.GEN_AI_OPERATION_NAME) != "invoke_agent":
            return
        name = attributes.get(_AGENT_NAME)
        if not isinstance(name, str) or not name:
            return
        with self._lock:
            names = self._agents_by_trace.get(trace_id)
            if names is None:
                names = self._agents_by_trace[trace_id] = set()
                while len(self._agents_by_trace) > self._max_tracked_traces:
                    self._agents_by_trace.popitem(last=False)
            else:
                self._agents_by_trace.move_to_end(trace_id)
            names.add(name)

    def _agents_in(self, trace_id: int) -> FrozenSet[str]:
        with self._lock:
            return frozenset(self._agents_by_trace.get(trace_id, ()))

    def update_config(self, config: TraceConfig) -> bool:
        """Replace the ``TraceConfig`` applied to later spans; ``True`` if it changed."""
        with self._lock:
            if config == self._config:
                return False
            self._config = config
            return True

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        """Copy traceAI context attributes (``using_session``, ``using_user``,
        ``using_metadata``, ``using_attributes``, ...) onto AG2 spans.

        A key AG2 already set is not overridden, and a key AG2 sets later wins,
        except ``session.id``: a session from the context replaces one from
        ``span_attributes`` (re-applied in ``on_end``).
        """
        if self._shutdown or _scope_name(span) != AG2_INSTRUMENTATION_SCOPE:
            return
        try:
            context_attributes = dict(get_attributes_from_context())
            if not context_attributes:
                return
            present = span.attributes or {}
            for key, value in context_attributes.items():
                if key == _SESSION_ID or key not in present:
                    span.set_attribute(key, value)
            session = context_attributes.get(_SESSION_ID)
            if session is not None:
                with self._lock:
                    self._session_by_span[_span_key(span)] = session
                    while len(self._session_by_span) > self._max_pending_spans:
                        self._session_by_span.popitem(last=False)
        except Exception:  # pragma: no cover - defensive
            logger.debug("traceai-ag2: could not read context attributes for %r", getattr(span, "name", None), exc_info=True)

    def on_end(self, span: ReadableSpan) -> None:
        if self._shutdown:
            return
        if _scope_name(span) != AG2_INSTRUMENTATION_SCOPE:
            return
        try:
            with self._lock:
                session = self._session_by_span.pop(_span_key(span), None)
            current = dict(span.attributes or {})
            trace_id = span.context.trace_id if span.context is not None else 0
            self._note_agent(trace_id, current)
            agents = self._agents_in(trace_id) if current.get("ag2.usage.kind") == _SUBTASK_USAGE_KIND else frozenset()
            mapped = normalize_attributes(current, self._config, instrumented_agents=agents)
            if session is not None:
                mapped[_SESSION_ID] = session
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
