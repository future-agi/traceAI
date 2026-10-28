"""Span processor that normalizes AG2 1.x ``TelemetryMiddleware`` spans.

AG2 1.x (PyPI ``ag2``, import ``ag2``) already emits OpenTelemetry GenAI spans
from ``ag2.middleware.builtin.telemetry.TelemetryMiddleware``. This processor
does not create spans. On every span from AG2's instrumentation scope it only:

* sets ``gen_ai.span.kind`` from ``gen_ai.operation.name`` for the operation
  strings AG2 emits that the Microsoft Agent Framework table also maps
  (``chat``, ``execute_tool``, ``invoke_agent``);
* copies AG2's cache usage keys onto the keys Future AGI reads
  (``gen_ai.usage.cache_read_tokens`` / ``cache_write_tokens``) and its thinking
  tokens onto ``gen_ai.usage.reasoning.output_tokens``, keeping the originals;
* on ``record_usage`` spans whose tokens a chat span in the same trace already
  carries (every ``model_call``; a ``subtask`` rollup whose sub-agent is itself
  instrumented in that trace), moves ``gen_ai.usage.input_tokens`` /
  ``output_tokens`` / ``total_tokens`` to ``ag2.usage.*`` so the trace total
  counts each model call once. ``aggregation`` and ``compaction`` usage keeps
  its promoted tokens: AG2 calls the model outside the middleware for those;
* applies ``TraceConfig`` (the traceAI privacy controls). AG2 records message
  content as one JSON string per span, so ``hide_inputs`` /
  ``hide_input_text`` drop every input-side content key (input messages,
  system instructions, tool arguments, human-input prompt), ``hide_outputs`` /
  ``hide_output_text`` every output-side one (output messages, tool result,
  human-input response), and ``hide_input_messages`` /
  ``hide_output_messages`` the message keys only. ``hide_input_images`` and
  ``hide_embedding_vectors`` have nothing to act on (AG2 records neither),
  and neither do ``hide_llm_invocation_parameters`` (AG2 sets no
  ``gen_ai.request.parameters``) and ``base64_image_max_length``;
  ``TraceConfig.mask`` still runs on every key;
* copies captured content to ``input.value`` / ``output.value`` (chat
  messages, tool arguments and result, human input), which is where Future
  AGI reads a span's input and output; content the config hides is not copied.
  A single chat completion goes on ``output.value`` as its message object, and
  ``invoke_agent`` gets its turn's first chat input and last chat output;
* on ``anthropic`` spans whose tokens count, adds the cache read and write
  tokens to ``gen_ai.usage.input_tokens`` (AG2's Anthropic mapper leaves them
  out; its OpenAI mapper and the GenAI conventions include them);
* in ``on_start``, copies traceAI context attributes (``using_session``,
  ``using_user``, ``using_metadata``, ``using_attributes``, ...) onto the span
  without overriding keys AG2 sets, except ``session.id``, where the context
  wins.

Spans from any other instrumentation scope pass through untouched.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from typing import AbstractSet, Any, Dict, FrozenSet, Mapping, Optional, Set, Tuple

from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from fi_instrumentation.instrumentation.config import TraceConfig
from fi_instrumentation.instrumentation.context_attributes import CONTEXT_ATTRIBUTES, get_attributes_from_context
from opentelemetry.attributes import BoundedAttributes
from opentelemetry.context import Context
from opentelemetry.sdk.trace import Event, ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import Status, StatusCode

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

# AG2 spelling -> the spelling Future AGI reads. AG2 sets the left-hand keys at
# telemetry.py lines 441-446 (record_usage span) and 505-510 (chat span) at
# 1.1.2, only when the usage field is non-zero. Originals are kept. The cache
# targets are the keys Future AGI's attribute registry reads
# (tracer/utils/semantic_conventions.py CACHE_READ_TOKENS / CACHE_WRITE_TOKENS,
# also fi_instrumentation SpanAttributes.GEN_AI_USAGE_CACHE_*_TOKENS); it does
# not read the dotted semconv spellings (gen_ai.usage.cache_read.input_tokens).
_CACHE_READ_TOKENS = SpanAttributes.GEN_AI_USAGE_CACHE_READ_TOKENS  # gen_ai.usage.cache_read_tokens
_CACHE_WRITE_TOKENS = SpanAttributes.GEN_AI_USAGE_CACHE_WRITE_TOKENS  # gen_ai.usage.cache_write_tokens
USAGE_KEY_ALIASES: Mapping[str, str] = {
    "gen_ai.usage.cache_creation_input_tokens": _CACHE_WRITE_TOKENS,
    "gen_ai.usage.cache_read_input_tokens": _CACHE_READ_TOKENS,
    "gen_ai.usage.thinking_tokens": "gen_ai.usage.reasoning.output_tokens",
}

# Content keys ``TraceConfig.mask`` does not fully cover. AG2 sets the tool
# and human-input keys only when ``capture_content=True`` (telemetry.py lines
# 541, 555, 578, 588 at 1.1.2) and records message content as one JSON string
# per span (``gen_ai.input.messages`` line 477, ``gen_ai.output.messages`` line
# 517), never as the flattened ``gen_ai.input.messages.{i}.message.content``
# keys that ``mask``'s ``hide_input_text`` / ``hide_output_text`` /
# ``hide_input_images`` rules match. So the text flags drop the whole JSON
# attribute. ``gen_ai.system_instructions`` is not emitted by ag2 1.0.0-1.1.2;
# it is listed so a later release that adds it is still hidden.
_INPUT_MESSAGES = SpanAttributes.GEN_AI_INPUT_MESSAGES  # gen_ai.input.messages
_OUTPUT_MESSAGES = SpanAttributes.GEN_AI_OUTPUT_MESSAGES  # gen_ai.output.messages
_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
_TOOL_ARGUMENTS = SpanAttributes.GEN_AI_TOOL_CALL_ARGUMENTS  # gen_ai.tool.call.arguments
_TOOL_RESULT = SpanAttributes.GEN_AI_TOOL_CALL_RESULT  # gen_ai.tool.call.result
_HUMAN_INPUT_PROMPT = "ag2.human_input.prompt"
_HUMAN_INPUT_RESPONSE = "ag2.human_input.response"
_INPUT_MESSAGE_KEYS = (_INPUT_MESSAGES, _SYSTEM_INSTRUCTIONS)
_OUTPUT_MESSAGE_KEYS = (_OUTPUT_MESSAGES,)
_INPUT_CONTENT_KEYS = _INPUT_MESSAGE_KEYS + (_TOOL_ARGUMENTS, _HUMAN_INPUT_PROMPT)
_OUTPUT_CONTENT_KEYS = _OUTPUT_MESSAGE_KEYS + (_TOOL_RESULT, _HUMAN_INPUT_RESPONSE)

# Future AGI fills a span's input and output only from ``input.value`` /
# ``output.value``; AG2 sets neither. Per operation, the content keys that
# become them. AG2 records no content on ``invoke_agent`` spans.
_IO_SOURCES: Mapping[str, Tuple[str, str]] = {
    "chat": (_INPUT_MESSAGES, _OUTPUT_MESSAGES),
    "execute_tool": (_TOOL_ARGUMENTS, _TOOL_RESULT),
    "await_human_input": (_HUMAN_INPUT_PROMPT, _HUMAN_INPUT_RESPONSE),
}


# --- local paths and stack traces are never exported ------------------------
#
# A failing tool's text is sent back to the model by AG2 and recorded as a
# tool message in the next chat span's input; for an exception that text is a
# full Python traceback with absolute file paths and the user's home
# directory. Span status descriptions and exception events carry
# ``str(exc)`` and ``exception.stacktrace``. None of that is exported in
# either privacy mode: tracebacks are cut, absolute local paths are replaced.
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\):.*", re.DOTALL)
_PATH_RE = re.compile(
    r"(?<![\w:/.])(?:[A-Za-z]:\\[^\s\"'<>|:*?]+|/(?:Users|home|root|private|var|tmp|opt|usr|srv|mnt|app|workspace|etc)/[^\s\"'<>|:*?,;)\]]+)"
)
STACK_TRACE_REMOVED = "[stack trace removed]"
PATH_REMOVED = "[path]"


def scrub_text(value: str) -> str:
    """Cut a Python traceback and replace absolute local file paths."""
    if "Traceback (most recent call last)" in value:
        value = _TRACEBACK_RE.sub(STACK_TRACE_REMOVED, value)
    return _PATH_RE.sub(PATH_REMOVED, value)


def _scrub_json(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, list):
        return [_scrub_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_json(v) for k, v in value.items()}
    return value


def _scrub_content_value(value: Any) -> Any:
    """Scrub a content attribute: plain text, or AG2's JSON-string messages."""
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except ValueError:
        return scrub_text(value)
    if isinstance(parsed, (dict, list)):
        scrubbed = _scrub_json(parsed)
        return value if scrubbed == parsed else json.dumps(scrubbed, ensure_ascii=False)
    return scrub_text(value)


def _scrub_paths_and_traces(mapped: Dict[str, Any]) -> None:
    for key in _INPUT_CONTENT_KEYS + _OUTPUT_CONTENT_KEYS:
        if key in mapped:
            mapped[key] = _scrub_content_value(mapped[key])


def _sanitize_error_details(span: ReadableSpan) -> None:
    """Drop raw exception text from the span status and exception events.

    The status keeps ``ERROR`` and gets the exception class name (from the
    exception event AG2 records) as its description instead of ``str(exc)``;
    exception events keep ``exception.type`` only.
    """
    events = list(span.events or ())
    error_type: Optional[str] = None
    new_events = []
    changed = False
    for event in events:
        attrs = dict(event.attributes or {})
        if event.name == "exception":
            error_type = error_type or (str(attrs["exception.type"]) if attrs.get("exception.type") else None)
            kept = {k: v for k, v in attrs.items() if k == "exception.type"}
            if kept != attrs:
                changed = True
                event = Event(name=event.name, attributes=kept, timestamp=event.timestamp)
        new_events.append(event)
    if changed:
        span._events = new_events  # type: ignore[attr-defined]
    status = span.status
    if status is not None and status.status_code == StatusCode.ERROR and status.description != error_type:
        span._status = Status(StatusCode.ERROR, error_type)  # type: ignore[attr-defined]


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
    _count_cached_input(mapped)

    _scrub_paths_and_traces(mapped)
    hidden = _hidden_content_keys(config) if config is not None else set()
    _copy_content_to_io(mapped, hidden)

    if config is not None:
        mapped = _apply_trace_config(mapped, config, hidden)
    return mapped


def _mime_type(value: Any) -> str:
    if isinstance(value, str):
        try:
            if isinstance(json.loads(value), (dict, list)):
                return "application/json"
        except ValueError:
            pass
    return "text/plain"


def _single_completion(value: Any) -> Any:
    """A one-message output list as that message object, else ``value`` unchanged.

    Observe's chat view shows an output object ``{role, content}`` as the
    assistant reply, and nothing for a list.
    """
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        return json.dumps(parsed[0], ensure_ascii=False)
    return value


def _copy_content_to_io(mapped: Dict[str, Any], hidden: AbstractSet[str]) -> None:
    """Copy captured content to ``input.value`` / ``output.value``.

    Only content AG2 captured and ``hidden`` does not remove is copied, so a
    hide flag also keeps the copy off the span. Existing values are kept.
    """
    operation = mapped.get(SpanAttributes.GEN_AI_OPERATION_NAME)
    sources = _IO_SOURCES.get(operation)  # type: ignore[arg-type]
    if sources is None:
        return
    targets = (
        (SpanAttributes.INPUT_VALUE, SpanAttributes.INPUT_MIME_TYPE),
        (SpanAttributes.OUTPUT_VALUE, SpanAttributes.OUTPUT_MIME_TYPE),
    )
    for source, (value_key, mime_key) in zip(sources, targets):
        value = mapped.get(source)
        if value is None or source in hidden or value_key in mapped:
            continue
        if operation == "chat" and value_key == SpanAttributes.OUTPUT_VALUE:
            value = _single_completion(value)
        mapped[value_key] = value
        mapped.setdefault(mime_key, _mime_type(value))


# AG2's Anthropic mapper (ag2/config/anthropic/mappers.py normalize_usage at
# 1.1.2) reports prompt_tokens as Anthropic's raw input_tokens, which leaves
# out cache reads and writes; its OpenAI mapper counts cached tokens inside
# prompt_tokens, and so do the GenAI conventions for gen_ai.usage.input_tokens.
_ANTHROPIC = "anthropic"
_CACHED_INPUT_KEYS = ("gen_ai.usage.cache_read_input_tokens", "gen_ai.usage.cache_creation_input_tokens")


def _token_count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _count_cached_input(mapped: Dict[str, Any]) -> None:
    """Add cached tokens to an ``anthropic`` span's counted input tokens.

    Only spans that still carry ``gen_ai.usage.input_tokens`` (chat spans and
    usage spans that are the only copy) change; a duplicate usage span's
    ``ag2.usage.*`` keeps AG2's own numbers. The total is recomputed when the
    span has one. The trace total then exceeds AG2's ``UsageReport`` by the
    cached tokens, which that report leaves out for Anthropic.
    """
    if mapped.get(SpanAttributes.GEN_AI_PROVIDER_NAME) != _ANTHROPIC:
        return
    uncached = _token_count(mapped.get("gen_ai.usage.input_tokens"))
    if uncached is None:
        return
    cached = sum(_token_count(mapped.get(key)) or 0 for key in _CACHED_INPUT_KEYS)
    if not cached:
        return
    mapped["gen_ai.usage.input_tokens"] = uncached + cached
    output = _token_count(mapped.get("gen_ai.usage.output_tokens"))
    if "gen_ai.usage.total_tokens" in mapped and output is not None:
        mapped["gen_ai.usage.total_tokens"] = uncached + cached + output


# fi-collector promotes these keys into the token columns on any span
# (fi-collector/pkg/adapter/adapter.go inputTokenKeys/outputTokenKeys/
# totalTokenKeys), and Observe sums total_tokens over every span of a trace.
_PROMOTED_TOKEN_KEYS = (
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.total_tokens",
    # Not summed today, but a duplicate usage span repeats the chat span's
    # cache counts too; keep Future AGI's cache keys on one span per call.
    _CACHE_READ_TOKENS,
    _CACHE_WRITE_TOKENS,
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


def _hidden_content_keys(config: TraceConfig) -> Set[str]:
    """Keys the config hides on AG2 spans, beyond what ``TraceConfig.mask`` does.

    * ``hide_inputs`` / ``hide_input_text``: every input-side text key
      (input messages, system instructions, tool arguments, human prompt).
    * ``hide_outputs`` / ``hide_output_text``: every output-side text key
      (output messages, tool result, human response).
    * ``hide_input_messages`` / ``hide_output_messages``: the message keys only.
    """
    hidden: Set[str] = set()
    if config.hide_inputs or config.hide_input_text:
        hidden.update(_INPUT_CONTENT_KEYS)
    elif config.hide_input_messages:
        hidden.update(_INPUT_MESSAGE_KEYS)
    if config.hide_outputs or config.hide_output_text:
        hidden.update(_OUTPUT_CONTENT_KEYS)
    elif config.hide_output_messages:
        hidden.update(_OUTPUT_MESSAGE_KEYS)
    return hidden


def _apply_trace_config(
    attributes: Dict[str, Any], config: TraceConfig, hidden: Optional[AbstractSet[str]] = None
) -> Dict[str, Any]:
    if hidden is None:
        hidden = _hidden_content_keys(config)

    masked: Dict[str, Any] = {}
    for key, value in attributes.items():
        if key in hidden:
            continue
        value = config.mask(key, value)
        if value is None:
            continue
        masked[key] = value
    return masked


def _move_context_keys_last(mapped: Dict[str, Any]) -> None:
    """Move traceAI context keys (``session.id``, ``user.id``, ...) to the newest end.

    ``on_start`` writes them before AG2 sets any attribute, so they are the
    oldest keys on the span, and ``BoundedAttributes`` evicts oldest-first
    when the keys this processor adds overflow ``max_span_attributes``.
    Moved last, they are evicted after every key AG2 set.
    """
    for key in CONTEXT_ATTRIBUTES:
        if key in mapped:
            mapped[key] = mapped.pop(key)


def _bounded_like(previous: Any, attributes: Dict[str, Any]) -> Any:
    """Return ``attributes`` in the container the span already used.

    The SDK keeps span attributes in ``BoundedAttributes``, and
    ``ReadableSpan.dropped_attributes`` (exported as
    ``dropped_attributes_count``) reads its ``dropped`` counter, which a plain
    dict would reset to 0. The span's limits and immutability are kept and the
    count is carried over. Keys this processor adds count against the same
    ``max_span_attributes`` limit: when the span is full the oldest key is
    evicted and counted, as the SDK does for ``set_attribute``.
    """
    if not isinstance(previous, BoundedAttributes):
        return attributes
    bounded = BoundedAttributes(
        maxlen=previous.maxlen,
        attributes=attributes,
        immutable=getattr(previous, "_immutable", True),
        max_value_len=previous.max_value_len,
    )
    bounded.dropped += previous.dropped
    return bounded


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
        # Per invoke_agent span (keyed by its trace and span id): the first
        # chat input and the last chat output of its turn, as
        # {"input": (start, value, mime), "output": (end, value, mime)}.
        self._agent_io: "OrderedDict[Tuple[int, int], Dict[str, Tuple[int, Any, Any]]]" = OrderedDict()
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
            operation = mapped.get(SpanAttributes.GEN_AI_OPERATION_NAME)
            if operation == "chat":
                self._note_chat_io(span, mapped)
            elif operation == "invoke_agent":
                self._apply_agent_io(span, mapped)
            if session is not None:
                mapped[_SESSION_ID] = session
            _move_context_keys_last(mapped)
            _sanitize_error_details(span)
            if mapped != current:
                # ``ReadableSpan.attributes`` is a read-only view over
                # ``_attributes``; every later processor and exporter in the
                # chain receives this same object.
                span._attributes = _bounded_like(span._attributes, mapped)  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive
            logger.debug("traceai-ag2: could not normalize span %r", getattr(span, "name", None), exc_info=True)

    def _note_chat_io(self, span: ReadableSpan, mapped: Mapping[str, Any]) -> None:
        """Remember a chat span's input / output for its parent ``invoke_agent``.

        AG2 records no content on ``invoke_agent``. The turn's first chat
        input and last chat output make it show the question and the answer.
        Only content that is on the chat span (captured, not hidden) is used.
        """
        parent = span.parent
        if parent is None or parent.is_remote:
            return
        found_input = mapped.get(SpanAttributes.INPUT_VALUE)
        found_output = mapped.get(SpanAttributes.OUTPUT_VALUE)
        if found_input is None and found_output is None:
            return
        key = (parent.trace_id, parent.span_id)
        with self._lock:
            entry = self._agent_io.get(key)
            if entry is None:
                entry = self._agent_io[key] = {}
                while len(self._agent_io) > self._max_pending_spans:
                    self._agent_io.popitem(last=False)
            if found_input is not None and span.start_time is not None:
                first = entry.get("input")
                if first is None or span.start_time < first[0]:
                    entry["input"] = (span.start_time, found_input, mapped.get(SpanAttributes.INPUT_MIME_TYPE))
            if found_output is not None and span.end_time is not None:
                last = entry.get("output")
                if last is None or span.end_time >= last[0]:
                    entry["output"] = (span.end_time, found_output, mapped.get(SpanAttributes.OUTPUT_MIME_TYPE))

    def _apply_agent_io(self, span: ReadableSpan, mapped: Dict[str, Any]) -> None:
        with self._lock:
            entry = self._agent_io.pop(_span_key(span), None)
        if not entry:
            return
        for side, value_key, mime_key in (
            ("input", SpanAttributes.INPUT_VALUE, SpanAttributes.INPUT_MIME_TYPE),
            ("output", SpanAttributes.OUTPUT_VALUE, SpanAttributes.OUTPUT_MIME_TYPE),
        ):
            found = entry.get(side)
            if found is None or value_key in mapped:
                continue
            _, value, mime = found
            mapped[value_key] = value
            if mime is not None:
                mapped.setdefault(mime_key, mime)

    def shutdown(self) -> None:
        self._shutdown = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
