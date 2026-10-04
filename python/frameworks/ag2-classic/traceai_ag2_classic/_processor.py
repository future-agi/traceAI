"""Span processor that normalizes AG2 Classic spans to Future AGI conventions.

``autogen`` 0.14.x ships its own OpenTelemetry instrumentation
(``autogen.opentelemetry``). Its spans already use most GenAI semantic
convention keys. This processor adds only what Future AGI needs and enforces
the content default. Every key below was read from the installed
``autogen/opentelemetry/instrumentators/`` at 0.14.1 (see README "Attribute
inventory" for file:line evidence).

What it does, per span on the ``opentelemetry.instrumentation.ag2`` scope:

* ``gen_ai.span.kind`` from upstream ``ag2.span.type`` (the ``SpanType`` enum).
* ``session.id`` from ``gen_ai.conversation.id`` on the outermost
  ``conversation`` span only. Nested chats (for example group-chat speaker
  selection) get their own ``chat_id`` upstream; those are not copied, so one
  run maps to one session.
* ``gen_ai.cost.total`` from upstream ``gen_ai.usage.cost`` on LLM spans, and
  ``gen_ai.usage.total_tokens`` = input + output on LLM spans.
* ``gen_ai.request.parameters`` JSON from upstream ``gen_ai.request.*``
  sampling keys.
* Status ``ERROR`` when upstream set ``error.type`` but left the status unset
  (tool failures are caught inside ``ConversableAgent.execute_function``, so
  upstream never sets the status itself).
* Content: off by default. Upstream agent, conversation, tool, human-input and
  code-execution spans always record message bodies, tool arguments/results,
  prompts and code output; there is no upstream flag for those. With
  ``capture_content=False`` (default) those keys are removed. With
  ``capture_content=True`` they are kept and lifted into ``input.value`` /
  ``output.value``.

Spans from any other instrumentation scope pass through untouched. The
processor never raises into the SDK.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, Mapping, Optional

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import Status, StatusCode

from fi_instrumentation.fi_types import FiMimeTypeValues, FiSpanKindValues, SpanAttributes

logger = logging.getLogger(__name__)

# Read from autogen/opentelemetry/consts.py at 0.14.1 (INSTRUMENTING_MODULE_NAME).
AG2_SCOPE = "opentelemetry.instrumentation.ag2"
# Set on every upstream span by the instrumentators (e.g. llm_wrapper.py:74).
SPAN_TYPE_KEY = "ag2.span.type"

# SpanType values (consts.py:13-22) -> Future AGI span kind.
KIND_BY_SPAN_TYPE: Dict[str, str] = {
    "llm": FiSpanKindValues.LLM.value,
    "agent": FiSpanKindValues.AGENT.value,
    "tool": FiSpanKindValues.TOOL.value,
    "code_execution": FiSpanKindValues.TOOL.value,
    "conversation": FiSpanKindValues.CHAIN.value,
    "multi_conversation": FiSpanKindValues.CHAIN.value,
    "speaker_selection": FiSpanKindValues.CHAIN.value,
    "human_input": FiSpanKindValues.CHAIN.value,
    # Declared upstream with a TODO and never emitted at 0.14.1. Kept as CHAIN.
    "handoff": FiSpanKindValues.CHAIN.value,
}

# Upstream keys (verbatim strings from the instrumentators).
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"
TOOL_ARGUMENTS = "gen_ai.tool.call.arguments"
TOOL_RESULT = "gen_ai.tool.call.result"
HUMAN_PROMPT = "ag2.human_input.prompt"
HUMAN_RESPONSE = "ag2.human_input.response"
CODE_OUTPUT = "ag2.code_execution.output"
CHAT_SUMMARIES = "ag2.chats.summaries"
CONVERSATION_ID = "gen_ai.conversation.id"
UPSTREAM_COST = "gen_ai.usage.cost"
ERROR_TYPE = "error.type"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

# Every key that carries user or model content. Removed unless capture is on.
CONTENT_KEYS = (
    INPUT_MESSAGES,
    OUTPUT_MESSAGES,
    TOOL_ARGUMENTS,
    TOOL_RESULT,
    HUMAN_PROMPT,
    HUMAN_RESPONSE,
    CODE_OUTPUT,
    CHAT_SUMMARIES,
    SpanAttributes.INPUT_VALUE,
    SpanAttributes.INPUT_MIME_TYPE,
    SpanAttributes.OUTPUT_VALUE,
    SpanAttributes.OUTPUT_MIME_TYPE,
)

# utils.py set_llm_request_params (lines 213-219).
REQUEST_PARAMETER_KEYS = (
    "gen_ai.request.temperature",
    "gen_ai.request.max_tokens",
    "gen_ai.request.top_p",
    "gen_ai.request.frequency_penalty",
    "gen_ai.request.presence_penalty",
)

# Only a live ``conversation`` ancestor makes a conversation "nested". Chats
# started by ``initiate_chats`` (``multi_conversation``) stay roots: each one is
# its own upstream chat with its own ``chat_id``.
_CONVERSATION_TYPES = ("conversation",)

# Upper bound on live AG2 spans tracked for the nested-conversation check, so a
# span that is started and never ended cannot grow memory without limit.
_MAX_LIVE_SPANS = 10000


def kind_for_span_type(span_type: Any) -> Optional[str]:
    """Return the Future AGI span kind for an upstream ``ag2.span.type`` value."""
    if not isinstance(span_type, str):
        return None
    return KIND_BY_SPAN_TYPE.get(span_type)


def _json_mime(value: str) -> str:
    stripped = value.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    ):
        return FiMimeTypeValues.JSON.value
    return FiMimeTypeValues.TEXT.value


def _set_io(mapped: Dict[str, Any], value_key: str, mime_key: str, value: Any) -> None:
    if value is None or value_key in mapped:
        return
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    mapped[value_key] = text
    mapped[mime_key] = _json_mime(text)


def _surface_content(mapped: Dict[str, Any], span_type: str) -> None:
    if span_type == "tool":
        _set_io(mapped, SpanAttributes.INPUT_VALUE, SpanAttributes.INPUT_MIME_TYPE, mapped.get(TOOL_ARGUMENTS))
        _set_io(mapped, SpanAttributes.OUTPUT_VALUE, SpanAttributes.OUTPUT_MIME_TYPE, mapped.get(TOOL_RESULT))
    elif span_type == "human_input":
        _set_io(mapped, SpanAttributes.INPUT_VALUE, SpanAttributes.INPUT_MIME_TYPE, mapped.get(HUMAN_PROMPT))
        _set_io(mapped, SpanAttributes.OUTPUT_VALUE, SpanAttributes.OUTPUT_MIME_TYPE, mapped.get(HUMAN_RESPONSE))
    elif span_type == "code_execution":
        _set_io(mapped, SpanAttributes.OUTPUT_VALUE, SpanAttributes.OUTPUT_MIME_TYPE, mapped.get(CODE_OUTPUT))
    elif span_type == "multi_conversation":
        _set_io(mapped, SpanAttributes.OUTPUT_VALUE, SpanAttributes.OUTPUT_MIME_TYPE, mapped.get(CHAT_SUMMARIES))
    else:
        _set_io(mapped, SpanAttributes.INPUT_VALUE, SpanAttributes.INPUT_MIME_TYPE, mapped.get(INPUT_MESSAGES))
        _set_io(mapped, SpanAttributes.OUTPUT_VALUE, SpanAttributes.OUTPUT_MIME_TYPE, mapped.get(OUTPUT_MESSAGES))


def _bundle_request_parameters(mapped: Dict[str, Any]) -> None:
    if SpanAttributes.GEN_AI_REQUEST_PARAMETERS in mapped:
        return
    params = {
        key.split("gen_ai.request.", 1)[1]: mapped[key] for key in REQUEST_PARAMETER_KEYS if key in mapped
    }
    if params:
        mapped[SpanAttributes.GEN_AI_REQUEST_PARAMETERS] = json.dumps(params)


def map_ag2_attributes(
    attributes: Mapping[str, Any],
    *,
    capture_content: bool = False,
    root_conversation: bool = True,
) -> Dict[str, Any]:
    """Return a new attribute dict with Future AGI keys added.

    ``root_conversation`` is False for a ``conversation`` span nested inside
    another live AG2 conversation; its ``gen_ai.conversation.id`` is then not
    copied to ``session.id``.

    Attributes without an upstream ``ag2.span.type`` are returned unchanged.
    """
    mapped = dict(attributes or {})
    span_type = mapped.get(SPAN_TYPE_KEY)
    kind = kind_for_span_type(span_type)
    if kind is None:
        return mapped

    mapped[SpanAttributes.GEN_AI_SPAN_KIND] = kind

    conversation_id = mapped.get(CONVERSATION_ID)
    if (
        span_type == "conversation"
        and root_conversation
        and conversation_id
        and SpanAttributes.SESSION_ID not in mapped
    ):
        mapped[SpanAttributes.SESSION_ID] = str(conversation_id)

    if kind == FiSpanKindValues.LLM.value:
        cost = mapped.get(UPSTREAM_COST)
        if isinstance(cost, (int, float)) and SpanAttributes.GEN_AI_COST_TOTAL not in mapped:
            mapped[SpanAttributes.GEN_AI_COST_TOTAL] = cost
        inp = mapped.get(INPUT_TOKENS)
        out = mapped.get(OUTPUT_TOKENS)
        if (
            isinstance(inp, int)
            and isinstance(out, int)
            and SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS not in mapped
        ):
            mapped[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] = inp + out
        _bundle_request_parameters(mapped)
    else:
        # fi-collector promotes these keys into the token columns on any span
        # (adapter.go inputTokenKeys/outputTokenKeys/totalTokenKeys) and Observe
        # sums total_tokens over a trace. Only LLM spans carry per-call usage;
        # the conversation span's chat-wide sum (chat.py:81-82) would double it.
        for key in (INPUT_TOKENS, OUTPUT_TOKENS, SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS):
            if key in mapped:
                mapped.setdefault("ag2.usage." + key.rsplit(".", 1)[1], mapped.pop(key))

    if capture_content:
        _surface_content(mapped, str(span_type))
    else:
        for key in CONTENT_KEYS:
            mapped.pop(key, None)

    return mapped


def _replace_attributes(span: ReadableSpan, mapped: Dict[str, Any]) -> None:
    """Write ``mapped`` back onto the ended span so later processors see it.

    ``ReadableSpan.attributes`` is a read-only view over ``span._attributes``.
    The SDK passes one ReadableSpan object to every processor in order, so
    replacing ``_attributes`` here is visible to the exporting processor that
    runs after this one. ``BoundedAttributes`` limits are preserved.
    """
    old = getattr(span, "_attributes", None)
    try:
        from opentelemetry.attributes import BoundedAttributes

        if isinstance(old, BoundedAttributes):
            new = BoundedAttributes(
                maxlen=old.maxlen,
                attributes=mapped,
                immutable=True,
                max_value_len=old.max_value_len,
            )
            new.dropped = old.dropped
            span._attributes = new  # type: ignore[attr-defined]
            return
    except Exception:  # pragma: no cover - fall back to a plain mapping
        pass
    span._attributes = mapped  # type: ignore[attr-defined]


class AG2ClassicSpanProcessor(SpanProcessor):
    """Normalize ``autogen.opentelemetry`` spans before they are exported.

    Install it ahead of the exporting processor (``setup()`` does this), so the
    exporter sees the mapped attributes.
    """

    def __init__(self, capture_content: bool = False) -> None:
        self.capture_content = bool(capture_content)
        self._live: Dict[int, Span] = {}
        self._lock = threading.Lock()
        self._disabled = False

    # SpanProcessor interface ----------------------------------------------

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        if self._disabled or not _is_ag2(span):
            return
        try:
            with self._lock:
                if len(self._live) < _MAX_LIVE_SPANS:
                    self._live[span.context.span_id] = span
        except Exception:  # pragma: no cover - never break the SDK
            return

    def on_end(self, span: ReadableSpan) -> None:
        if self._disabled or not _is_ag2(span):
            return
        try:
            with self._lock:
                self._live.pop(span.context.span_id, None)
            attributes = dict(span.attributes or {})
            root = True
            if attributes.get(SPAN_TYPE_KEY) == "conversation":
                root = not self._has_live_conversation_ancestor(span)
            mapped = map_ag2_attributes(
                attributes, capture_content=self.capture_content, root_conversation=root
            )
            if mapped != attributes:
                _replace_attributes(span, mapped)

            error_type = attributes.get(ERROR_TYPE)
            if error_type and span.status.status_code is StatusCode.UNSET:
                span._status = Status(StatusCode.ERROR, str(error_type))  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - never break the SDK
            logger.debug("traceai-ag2-classic: span mapping failed", exc_info=True)

    def shutdown(self) -> None:
        self._disabled = True
        try:
            with self._lock:
                self._live.clear()
        except Exception:  # pragma: no cover - never break SDK shutdown
            pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    # Internals --------------------------------------------------------------

    def _has_live_conversation_ancestor(self, span: ReadableSpan) -> bool:
        parent = span.parent
        seen = 0
        with self._lock:
            while parent is not None and seen < 256:
                live = self._live.get(parent.span_id)
                if live is None:
                    return False
                attrs = live.attributes or {}
                if attrs.get(SPAN_TYPE_KEY) in _CONVERSATION_TYPES:
                    return True
                parent = live.parent
                seen += 1
        return False


def _is_ag2(span: Any) -> bool:
    scope = getattr(span, "instrumentation_scope", None)
    return getattr(scope, "name", None) == AG2_SCOPE
