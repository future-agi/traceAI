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
    ToolCallAttributes,
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


def _flatten_message_parts(parts: Any, msg_index_prefix: str) -> Dict[str, Any]:
    """Walk a message's ``parts`` list and emit FI-flavored sub-attributes.

    Handles the part types the framework emits:
      * ``text``               -> appended to ``message.content``
      * ``tool_call_response`` -> appended to ``message.content`` + ``message.tool_call_id``
      * ``tool_call``          -> ``message.tool_calls.{j}.tool_call.{id,function.name,function.arguments}``

    ``msg_index_prefix`` is ``{messages_prefix}.{i}`` — e.g. ``gen_ai.output.messages.0``.
    """
    if not isinstance(parts, list):
        return {}
    out: Dict[str, Any] = {}
    texts: List[str] = []
    tool_call_idx = 0

    for part in parts:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")

        if ptype in ("text", "reasoning"):
            content = part.get("content")
            if isinstance(content, str):
                texts.append(content)

        elif ptype == "tool_call_response":
            response = part.get("response")
            if isinstance(response, str):
                texts.append(response)
            elif response is not None:
                texts.append(json.dumps(response))
            call_id = part.get("id")
            if call_id is not None:
                out[f"{msg_index_prefix}.{MessageAttributes.MESSAGE_TOOL_CALL_ID}"] = call_id

        elif ptype == "tool_call":
            tc_prefix = (
                f"{msg_index_prefix}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{tool_call_idx}"
            )
            call_id = part.get("id")
            name = part.get("name")
            arguments = part.get("arguments")
            if call_id is not None:
                out[f"{tc_prefix}.{ToolCallAttributes.TOOL_CALL_ID}"] = call_id
            if name is not None:
                out[f"{tc_prefix}.{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}"] = name
            if arguments is not None:
                out[f"{tc_prefix}.{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}"] = (
                    arguments if isinstance(arguments, str) else json.dumps(arguments)
                )
            tool_call_idx += 1

    if texts:
        out[f"{msg_index_prefix}.{MessageAttributes.MESSAGE_CONTENT}"] = "\n".join(texts)
    return out


def _flatten_messages(messages_json: str, prefix: str) -> Dict[str, Any]:
    """Flatten Agent Framework message JSON into per-index FI attributes.

    Input shape (what the framework emits):
        [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}, ...]

    Output keys:
        {prefix}.{i}.message.role
        {prefix}.{i}.message.content                    (text + tool_call_response text)
        {prefix}.{i}.message.tool_call_id               (when a tool_call_response is present)
        {prefix}.{i}.message.tool_calls.{j}.tool_call.* (for tool_call parts)
    """
    parsed = _safe_json_loads(messages_json)
    if not isinstance(parsed, list):
        return {}
    out: Dict[str, Any] = {}
    for i, msg in enumerate(parsed):
        if not isinstance(msg, dict):
            continue
        msg_index_prefix = f"{prefix}.{i}"
        role = msg.get("role")
        if role is not None:
            out[f"{msg_index_prefix}.{MessageAttributes.MESSAGE_ROLE}"] = role
        out.update(_flatten_message_parts(msg.get("parts"), msg_index_prefix))
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
    """Lift ``gen_ai.input/output.messages`` into ``input.value``/``output.value`` + flatten.

    The raw JSON is preserved on ``input.value``/``output.value`` (mime
    ``application/json``) so the framework's native message shape is retained,
    matching the openai/anthropic/litellm sibling adapters.
    """
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


def _bundle_request_parameters(mapped: Dict[str, Any]) -> None:
    """Collect ``gen_ai.request.*`` settings (temperature, top_p, max_tokens,
    choice.count, etc.) into a single ``gen_ai.request.parameters`` JSON so
    the FI dashboard's "Model Parameters" panel has one key to read.
    Excludes ``gen_ai.request.model`` — that's its own first-class attribute."""
    if SpanAttributes.GEN_AI_REQUEST_PARAMETERS in mapped:
        return
    params: Dict[str, Any] = {}
    for key, val in list(mapped.items()):
        if (
            isinstance(key, str)
            and key.startswith("gen_ai.request.")
            and key != SpanAttributes.GEN_AI_REQUEST_MODEL
        ):
            params[key.split("gen_ai.request.", 1)[1]] = val
    if params:
        mapped[SpanAttributes.GEN_AI_REQUEST_PARAMETERS] = json.dumps(params)


def _derive_total_tokens(mapped: Dict[str, Any]) -> None:
    """If input + output tokens are both present, compute the total."""
    if _TOTAL_TOKENS in mapped:
        return
    inp = mapped.get(_INPUT_TOKENS)
    out = mapped.get(_OUTPUT_TOKENS)
    if isinstance(inp, (int, float)) and isinstance(out, (int, float)):
        mapped[_TOTAL_TOKENS] = int(inp) + int(out)


def _add_graph_node_attrs(mapped: Dict[str, Any], kind: str) -> None:
    """Stamp ``graph.node.id`` / ``graph.node.name`` so the FI agent-graph view
    has stable identifiers for LLM / TOOL / AGENT spans."""
    if kind == FiSpanKindValues.LLM.value or kind == FiSpanKindValues.EMBEDDING.value:
        name = mapped.get("gen_ai.response.model") or mapped.get("gen_ai.request.model")
        node_id = mapped.get("gen_ai.response.id") or name
        if node_id is not None:
            mapped[SpanAttributes.GRAPH_NODE_ID] = f"llm_{node_id}"
        if name is not None:
            mapped[SpanAttributes.GRAPH_NODE_NAME] = name

    elif kind == FiSpanKindValues.AGENT.value:
        agent_id = mapped.get("gen_ai.agent.id")
        agent_name = mapped.get("gen_ai.agent.name")
        if agent_id is not None:
            mapped[SpanAttributes.GRAPH_NODE_ID] = f"agent_{agent_id}"
        if agent_name is not None:
            mapped[SpanAttributes.GRAPH_NODE_NAME] = agent_name

    elif kind == FiSpanKindValues.TOOL.value:
        tool_name = mapped.get("gen_ai.tool.name")
        call_id = mapped.get("gen_ai.tool.call.id")
        if tool_name is not None:
            mapped[SpanAttributes.GRAPH_NODE_NAME] = tool_name
            node_id = f"tool_{tool_name}" + (f"_{call_id}" if call_id else "")
            mapped[SpanAttributes.GRAPH_NODE_ID] = node_id

    elif kind == FiSpanKindValues.CHAIN.value:
        if workflow_id := mapped.get("workflow.id"):
            mapped[SpanAttributes.GRAPH_NODE_ID] = f"workflow_{workflow_id}"
            if workflow_name := mapped.get("workflow.name"):
                mapped[SpanAttributes.GRAPH_NODE_NAME] = workflow_name
        elif executor_id := mapped.get("executor.id"):
            mapped[SpanAttributes.GRAPH_NODE_ID] = f"executor_{executor_id}"
            if executor_type := mapped.get("executor.type"):
                mapped[SpanAttributes.GRAPH_NODE_NAME] = executor_type
        elif edge_group_id := mapped.get("edge_group.id"):
            mapped[SpanAttributes.GRAPH_NODE_ID] = f"edge_group_{edge_group_id}"
            if edge_group_type := mapped.get("edge_group.type"):
                mapped[SpanAttributes.GRAPH_NODE_NAME] = edge_group_type


_METADATA_KEYS = (
    "gen_ai.request.choice.count",
    "server.address",
    "agent_framework.function.invocation.duration",
    "agent_framework.function.name",
)


def _bundle_metadata(mapped: Dict[str, Any]) -> None:
    """Bundle a few miscellaneous attrs into a single ``metadata`` JSON string."""
    if "metadata" in mapped:
        return
    extras = {k: mapped[k] for k in _METADATA_KEYS if k in mapped}
    if extras:
        mapped["metadata"] = json.dumps(extras)


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
        _bundle_request_parameters(mapped)
    elif kind == FiSpanKindValues.TOOL.value:
        _enrich_tool(mapped)
    # CHAIN: stamped here; I/O bubbled in by AgentFrameworkSpanProcessor.on_end().

    _add_graph_node_attrs(mapped, kind)
    _bundle_metadata(mapped)

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
