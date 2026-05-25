"""
SpanProcessor for the Microsoft Agent Framework + Future AGI integration.

Microsoft Agent Framework emits OpenTelemetry spans using the GenAI semantic
conventions (``gen_ai.*``) on the ``"agent_framework"`` instrumentation scope.
Future AGI's ``SpanAttributes`` is built on the same conventions, so most
attributes pass through unchanged. This processor adds the few keys FI needs
that the framework doesn't emit:

  * ``gen_ai.span.kind`` (FI-specific) classified from operation name / attrs
  * ``input.value`` / ``output.value`` (+ mime types) for the FI dashboard
  * Flattened ``gen_ai.input.messages.{i}.message.role`` / ``.content`` from
    the framework's JSON-string ``gen_ai.input.messages`` / ``gen_ai.output.messages``
  * ``gen_ai.usage.total_tokens`` derived from input + output tokens when present
  * For CHAIN spans (``workflow.run``, ``executor.process``, etc.) that the
    framework leaves without I/O, bubbles ``input.value`` / ``output.value``
    up from the earliest / latest descendant span.
"""

import json
import threading
from typing import Any, Dict, List, Optional

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span
from opentelemetry.sdk.trace import SpanProcessor

from fi_instrumentation.fi_types import (
    FiMimeTypeValues,
    FiSpanKindValues,
    MessageAttributes,
    SpanAttributes,
)

# ---------------------------------------------------------------------------
# Attribute keys we read from the framework's gen_ai.* output.
# ---------------------------------------------------------------------------

_OP = SpanAttributes.GEN_AI_OPERATION_NAME
_INPUT_MSGS = SpanAttributes.GEN_AI_INPUT_MESSAGES
_OUTPUT_MSGS = SpanAttributes.GEN_AI_OUTPUT_MESSAGES
_TOOL_ARGS = SpanAttributes.GEN_AI_TOOL_CALL_ARGUMENTS
_TOOL_RESULT = SpanAttributes.GEN_AI_TOOL_CALL_RESULT
_INPUT_TOKENS = SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS
_OUTPUT_TOKENS = SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS
_TOTAL_TOKENS = SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS

# The instrumentation scope Microsoft Agent Framework emits on.
_AGENT_FRAMEWORK_SCOPE = "agent_framework"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_json_loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_text_from_parts(parts: Any) -> Optional[str]:
    """Agent Framework messages carry content as ``parts: [{type, content}]``.

    Concatenates the text from ``type="text"`` parts. Non-text parts (image,
    blob, reasoning, function-call variants) are skipped; the raw JSON blob
    remains on the span as ``input.value`` / ``output.value`` so the
    information is not lost.
    """
    if not isinstance(parts, list):
        return None
    texts: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            content = part.get("content")
            if isinstance(content, str):
                texts.append(content)
    return "\n".join(texts) if texts else None


def _flatten_messages(messages_json: str, prefix: str) -> Dict[str, Any]:
    """Flatten Agent Framework message JSON into per-index FI attributes.

    Input shape (what the framework emits):
        [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}, ...]

    Output keys:
        {prefix}.{i}.message.role
        {prefix}.{i}.message.content   (joined text from text-type parts)
    """
    parsed = _safe_json_loads(messages_json)
    if not isinstance(parsed, list):
        return {}
    out: Dict[str, Any] = {}
    for i, msg in enumerate(parsed):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role is not None:
            out[f"{prefix}.{i}.{MessageAttributes.MESSAGE_ROLE}"] = role
        text = _extract_text_from_parts(msg.get("parts"))
        if text is not None:
            out[f"{prefix}.{i}.{MessageAttributes.MESSAGE_CONTENT}"] = text
    return out


_CHAIN_PREFIXES = ("workflow.", "workflow_builder.", "executor.", "edge_group.")
_CHAIN_KEYS = {"message.type", "message.source_id", "message.target_id"}


def _classify_span_kind(attributes: Dict[str, Any]) -> Optional[str]:
    """Return a FiSpanKindValues string, or ``None`` to leave the span alone."""
    op = attributes.get(_OP)
    if op == "chat":
        return FiSpanKindValues.LLM.value
    if op == "embeddings":
        return FiSpanKindValues.EMBEDDING.value
    if op == "execute_tool":
        return FiSpanKindValues.TOOL.value
    if op in ("invoke_agent", "create_agent"):
        return FiSpanKindValues.AGENT.value

    for key in attributes:
        if not isinstance(key, str):
            continue
        if key.startswith(_CHAIN_PREFIXES) or key in _CHAIN_KEYS:
            return FiSpanKindValues.CHAIN.value
    return None


# ---------------------------------------------------------------------------
# Per-kind enrichment
# ---------------------------------------------------------------------------


def _surface_messages_io(mapped: Dict[str, Any]) -> None:
    """Lift ``gen_ai.input/output.messages`` into ``input.value``/``output.value`` + flatten."""
    in_msgs = mapped.get(_INPUT_MSGS)
    if isinstance(in_msgs, str):
        mapped[SpanAttributes.INPUT_VALUE] = in_msgs
        mapped[SpanAttributes.INPUT_MIME_TYPE] = FiMimeTypeValues.JSON.value
        for k, v in _flatten_messages(in_msgs, _INPUT_MSGS).items():
            mapped[k] = v

    out_msgs = mapped.get(_OUTPUT_MSGS)
    if isinstance(out_msgs, str):
        mapped[SpanAttributes.OUTPUT_VALUE] = out_msgs
        mapped[SpanAttributes.OUTPUT_MIME_TYPE] = FiMimeTypeValues.JSON.value
        for k, v in _flatten_messages(out_msgs, _OUTPUT_MSGS).items():
            mapped[k] = v


def _derive_total_tokens(mapped: Dict[str, Any]) -> None:
    """If input + output tokens are both present, compute the total."""
    if _TOTAL_TOKENS in mapped:
        return
    inp = mapped.get(_INPUT_TOKENS)
    out = mapped.get(_OUTPUT_TOKENS)
    if isinstance(inp, (int, float)) and isinstance(out, (int, float)):
        mapped[_TOTAL_TOKENS] = int(inp) + int(out)


def _enrich_tool(mapped: Dict[str, Any]) -> None:
    """TOOL spans: lift tool call arguments and result into input/output."""
    args = mapped.get(_TOOL_ARGS)
    if args is not None:
        mapped[SpanAttributes.INPUT_VALUE] = (
            args if isinstance(args, str) else json.dumps(args)
        )
        mapped[SpanAttributes.INPUT_MIME_TYPE] = FiMimeTypeValues.JSON.value

    result = mapped.get(_TOOL_RESULT)
    if result is None:
        return
    if isinstance(result, (dict, list)):
        mapped[SpanAttributes.OUTPUT_VALUE] = json.dumps(result)
        mapped[SpanAttributes.OUTPUT_MIME_TYPE] = FiMimeTypeValues.JSON.value
    elif isinstance(result, str):
        mapped[SpanAttributes.OUTPUT_VALUE] = result
        stripped = result.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or (
            stripped.startswith("[") and stripped.endswith("]")
        ):
            mapped[SpanAttributes.OUTPUT_MIME_TYPE] = FiMimeTypeValues.JSON.value
        else:
            mapped[SpanAttributes.OUTPUT_MIME_TYPE] = FiMimeTypeValues.TEXT.value
    else:
        mapped[SpanAttributes.OUTPUT_VALUE] = str(result)


# ---------------------------------------------------------------------------
# Main per-span mapping function
# ---------------------------------------------------------------------------


def _map_attributes_to_fi_conventions(attributes: Dict[str, Any]) -> Dict[str, Any]:
    """Add FI-specific keys on top of the framework's native gen_ai.* attributes."""
    if not attributes:
        return {}
    mapped = dict(attributes)

    kind = _classify_span_kind(attributes)
    if kind is None:
        return mapped
    mapped[SpanAttributes.GEN_AI_SPAN_KIND] = kind

    if kind in (
        FiSpanKindValues.LLM.value,
        FiSpanKindValues.EMBEDDING.value,
        FiSpanKindValues.AGENT.value,
    ):
        _surface_messages_io(mapped)
        _derive_total_tokens(mapped)
    elif kind == FiSpanKindValues.TOOL.value:
        _enrich_tool(mapped)
    # CHAIN: stamped here; I/O bubbled in by AgentFrameworkSpanProcessor.on_end().

    return mapped


# ---------------------------------------------------------------------------
# Descendant-IO aggregation (for bubbling I/O up to CHAIN spans)
# ---------------------------------------------------------------------------


class _SpanIO:
    """Holds the earliest input and latest output among a span and its descendants."""

    __slots__ = ("input_value", "input_mime", "input_time",
                 "output_value", "output_mime", "output_time")

    def __init__(self) -> None:
        self.input_value: Optional[str] = None
        self.input_mime: Optional[str] = None
        self.input_time: Optional[int] = None
        self.output_value: Optional[str] = None
        self.output_mime: Optional[str] = None
        self.output_time: Optional[int] = None

    def absorb_input(self, value: Optional[str], mime: Optional[str], time_ns: Optional[int]) -> None:
        if value is None or time_ns is None:
            return
        if self.input_time is None or time_ns < self.input_time:
            self.input_value = value
            self.input_mime = mime
            self.input_time = time_ns

    def absorb_output(self, value: Optional[str], mime: Optional[str], time_ns: Optional[int]) -> None:
        if value is None or time_ns is None:
            return
        if self.output_time is None or time_ns > self.output_time:
            self.output_value = value
            self.output_mime = mime
            self.output_time = time_ns

    def merge(self, other: "_SpanIO") -> None:
        self.absorb_input(other.input_value, other.input_mime, other.input_time)
        self.absorb_output(other.output_value, other.output_mime, other.output_time)


# ---------------------------------------------------------------------------
# The SpanProcessor itself
# ---------------------------------------------------------------------------


class AgentFrameworkSpanProcessor(SpanProcessor):
    """OTel SpanProcessor that re-keys Agent Framework spans into FI conventions.

    On ``on_end``:
      1. Per-span enrichment: stamp ``gen_ai.span.kind`` and (for kinds with
         data) lift ``input.value``/``output.value`` + flatten messages.
      2. For CHAIN spans, bubble I/O in from descendant spans we've already seen.
      3. Propagate this span's "best I/O" up so its own parent can use it later.

    Spans from other instrumentation scopes pass through untouched.
    """

    def __init__(self) -> None:
        self._desc_io: Dict[int, _SpanIO] = {}
        self._lock = threading.Lock()
        self._disabled = False

    # SpanProcessor interface ------------------------------------------------

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        return

    def on_end(self, span: ReadableSpan) -> None:
        if self._disabled:
            return

        scope = getattr(span.instrumentation_scope, "name", None)
        if scope != _AGENT_FRAMEWORK_SCOPE:
            # Track parent relationship so descendant I/O from foreign spans
            # can still bubble into our CHAIN spans, but do not mutate attrs.
            self._track_for_parent(span, _attrs_dict(span))
            return

        try:
            current_attrs = _attrs_dict(span)
            mapped = _map_attributes_to_fi_conventions(current_attrs)

            if mapped.get(SpanAttributes.GEN_AI_SPAN_KIND) == FiSpanKindValues.CHAIN.value:
                self._apply_chain_bubble(span, mapped)

            # Write back onto the live ReadableSpan; ``span.attributes`` is a
            # MappingProxyType over ``span._attributes``, so downstream
            # processors and exporters will see the mutated dict.
            setattr(span, "_attributes", mapped)

            self._track_for_parent(span, mapped)
        except Exception:
            # Never crash the SDK over a mapping bug.
            return

    def shutdown(self) -> None:
        self._disabled = True
        with self._lock:
            self._desc_io.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    # Internals --------------------------------------------------------------

    def _apply_chain_bubble(self, span: ReadableSpan, mapped: Dict[str, Any]) -> None:
        """If this CHAIN span is missing I/O, fill from accumulated descendants."""
        sid = span.context.span_id
        with self._lock:
            bucket = self._desc_io.get(sid)
        if bucket is None:
            return

        if (
            SpanAttributes.INPUT_VALUE not in mapped
            and bucket.input_value is not None
        ):
            mapped[SpanAttributes.INPUT_VALUE] = bucket.input_value
            if bucket.input_mime:
                mapped[SpanAttributes.INPUT_MIME_TYPE] = bucket.input_mime

        if (
            SpanAttributes.OUTPUT_VALUE not in mapped
            and bucket.output_value is not None
        ):
            mapped[SpanAttributes.OUTPUT_VALUE] = bucket.output_value
            if bucket.output_mime:
                mapped[SpanAttributes.OUTPUT_MIME_TYPE] = bucket.output_mime

    def _track_for_parent(self, span: ReadableSpan, attrs: Dict[str, Any]) -> None:
        """Roll this span's I/O (plus any descendants') up to its parent's bucket."""
        parent = getattr(span, "parent", None)
        parent_id = getattr(parent, "span_id", None) if parent is not None else None

        sid = span.context.span_id
        with self._lock:
            own = self._desc_io.pop(sid, _SpanIO())

        own.absorb_input(
            attrs.get(SpanAttributes.INPUT_VALUE),
            attrs.get(SpanAttributes.INPUT_MIME_TYPE),
            span.start_time,
        )
        own.absorb_output(
            attrs.get(SpanAttributes.OUTPUT_VALUE),
            attrs.get(SpanAttributes.OUTPUT_MIME_TYPE),
            span.end_time,
        )

        if parent_id is None:
            return

        with self._lock:
            parent_bucket = self._desc_io.setdefault(parent_id, _SpanIO())
            parent_bucket.merge(own)


# ---------------------------------------------------------------------------
# Small helper
# ---------------------------------------------------------------------------


def _attrs_dict(span: ReadableSpan) -> Dict[str, Any]:
    attrs = getattr(span, "_attributes", None)
    if attrs is None:
        return {}
    if isinstance(attrs, dict):
        return attrs
    return dict(attrs)
