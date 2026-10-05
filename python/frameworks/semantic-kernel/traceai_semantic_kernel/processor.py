"""Mapping-only span processor for Semantic Kernel's native diagnostics spans.

Semantic Kernel (Python) creates its own OpenTelemetry spans. This processor
does not create spans and does not wrap Semantic Kernel code. It only edits
attributes on spans whose instrumentation scope is a ``semantic_kernel``
module, before the exporting processor sees them.

Upstream surface (semantic-kernel 1.38.0 through 1.44.1, files identical):

* ``chat <model>`` / ``text_completions <model>``:
  ``utils/telemetry/model_diagnostics/decorators.py`` (operation strings at
  lines 37-38, span attributes at 337-362, usage at 421-427, error at 450-453).
* ``invoke_agent <name>``: ``utils/telemetry/agent_diagnostics/decorators.py``
  (operation at line 35, attributes at 183-205, content at 208-227).
* ``execute_tool <plugin-function>``: ``functions/kernel_function.py`` lines
  264-288 through ``model_diagnostics/function_tracer.py`` lines 54-65. Emitted
  for every kernel function invocation. A function the model asked for (auto
  function invocation) runs as a direct child of ``AutoFunctionInvocationLoop``
  and carries ``gen_ai.tool.call.id`` when the connector supplies one
  (``kernel.py`` line 471 passes the function call content as metadata; the
  Ollama connector sends no id, ``ollama_chat_completion.py`` 244-253).
* ``AutoFunctionInvocationLoop``: ``connectors/ai/chat_completion_client_base.py``
  lines 137, 256 and 410-424 (``sk.available_functions`` only).

What the processor adds, per Semantic Kernel span:

* ``fi.span.kind`` and ``gen_ai.span.kind`` (fi-collector reads ``fi.span.kind``
  first, then ``gen_ai.span.kind``).
* ``gen_ai.provider.name`` copied from ``gen_ai.system`` when absent.
* ``gen_ai.usage.total_tokens`` on model-call spans when both parts exist.
* ``session.id`` from ``gen_ai.conversation.id`` when present and ``session.id``
  is not already set. Semantic Kernel 1.44.1 does not emit a conversation or
  thread id, so this is forward-compatible only.
* Context attributes from ``fi_instrumentation.using_attributes`` /
  ``using_session`` (session, user, metadata, tags) on span start, so app-set
  sessions reach native spans.
* Token and cost keys that fi-collector promotes into hot columns are kept on
  model-call (LLM) spans only. Any copy on another span kind is moved to
  ``semantic_kernel.usage.*`` so trace-wide sums are not inflated.
* Content: with ``sensitive=False`` (default) message, tool-argument and
  tool-result keys are removed. With ``sensitive=True`` they are kept and
  copied to ``input.value`` / ``output.value``, except that TraceConfig
  ``hide_inputs`` / ``hide_outputs`` (``FI_HIDE_INPUTS`` / ``FI_HIDE_OUTPUTS``)
  drop the input / output side, ``input.value`` / ``output.value`` included.
* Status ``ERROR`` when ``error.type`` is set but the status was left unset.
  An existing status is never cleared or downgraded.

The processor never raises into the OpenTelemetry SDK.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from opentelemetry import trace as trace_api
from opentelemetry.context import Context, get_value
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import Status, StatusCode

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import FiMimeTypeValues, FiSpanKindValues, SpanAttributes
from fi_instrumentation.instrumentation.context_attributes import CONTEXT_ATTRIBUTES

logger = logging.getLogger(__name__)

# Semantic Kernel creates its tracers with ``get_tracer(__name__)``, so every
# native span has a ``semantic_kernel.*`` instrumentation scope.
SK_SCOPE_PREFIX = "semantic_kernel"

# fi-collector spanKindAttrKeys (exporter/clickhouse25exporter/converter.go at
# future-agi d794a49b): "fi.span.kind" first, then "gen_ai.span.kind".
FI_SPAN_KIND = "fi.span.kind"
GEN_AI_SPAN_KIND = SpanAttributes.GEN_AI_SPAN_KIND  # "gen_ai.span.kind"

# Upstream keys (verbatim strings from the installed semantic-kernel).
OPERATION = "gen_ai.operation.name"
SYSTEM = "gen_ai.system"
MODEL = "gen_ai.request.model"
AGENT_NAME = "gen_ai.agent.name"
TOOL_NAME = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call.id"
TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
TOOL_CALL_RESULT = "gen_ai.tool.call.result"
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"
ERROR_TYPE = "error.type"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
TOTAL_TOKENS = SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS  # "gen_ai.usage.total_tokens"
CONVERSATION_ID = SpanAttributes.GEN_AI_CONVERSATION_ID  # "gen_ai.conversation.id"
PROVIDER_NAME = SpanAttributes.GEN_AI_PROVIDER_NAME  # "gen_ai.provider.name"
SESSION_ID = SpanAttributes.SESSION_ID  # "session.id"

AUTO_FUNCTION_INVOCATION_SPAN = "AutoFunctionInvocationLoop"  # semantic_kernel/const.py:11

# Operation strings read from the installed SDK (see module docstring).
KIND_BY_OPERATION: Dict[str, str] = {
    "chat": FiSpanKindValues.LLM.value,
    "text_completions": FiSpanKindValues.LLM.value,
    "invoke_agent": FiSpanKindValues.AGENT.value,
    # "execute_tool" is resolved in kind_for(): TOOL when the model asked for
    # it (tool call id, or a child of AutoFunctionInvocationLoop), else CHAIN.
}

# fi-collector promotes these into token/cost hot columns on ANY span kind
# (pkg/adapter/adapter.go inputTokenKeys/outputTokenKeys/totalTokenKeys/
# costTotalKeys/costInputKeys/costOutputKeys at future-agi d794a49b).
PROMOTED_USAGE_KEYS: Tuple[str, ...] = (
    "llm.token_count.prompt",
    "gen_ai.usage.input_tokens",
    "llm.usage.prompt_tokens",
    "llm.token_count.completion",
    "gen_ai.usage.output_tokens",
    "llm.usage.completion_tokens",
    "llm.token_count.total",
    "gen_ai.usage.total_tokens",
    "llm.usage.total_tokens",
    "gen_ai.cost.total",
    "llm.cost.total",
    "gen_ai.cost.input",
    "llm.cost.prompt",
    "gen_ai.cost.output",
    "llm.cost.completion",
)
USAGE_NAMESPACE = "semantic_kernel.usage."

# Keys that carry prompts, completions, tool arguments or tool results.
CONTENT_KEYS: Tuple[str, ...] = (
    INPUT_MESSAGES,
    OUTPUT_MESSAGES,
    TOOL_CALL_ARGUMENTS,
    TOOL_CALL_RESULT,
    SpanAttributes.INPUT_VALUE,
    SpanAttributes.INPUT_MIME_TYPE,
    SpanAttributes.OUTPUT_VALUE,
    SpanAttributes.OUTPUT_MIME_TYPE,
)
# The input and output halves of CONTENT_KEYS, dropped by TraceConfig
# hide_inputs / hide_outputs (FI_HIDE_INPUTS / FI_HIDE_OUTPUTS).
INPUT_CONTENT_KEYS: Tuple[str, ...] = (
    INPUT_MESSAGES,
    TOOL_CALL_ARGUMENTS,
    SpanAttributes.INPUT_VALUE,
    SpanAttributes.INPUT_MIME_TYPE,
)
OUTPUT_CONTENT_KEYS: Tuple[str, ...] = (
    OUTPUT_MESSAGES,
    TOOL_CALL_RESULT,
    SpanAttributes.OUTPUT_VALUE,
    SpanAttributes.OUTPUT_MIME_TYPE,
)


def kind_for(name: str, attributes: Mapping[str, Any], auto_invoked: bool = False) -> Optional[str]:
    """Return the Future AGI span kind for a Semantic Kernel span, or None.

    ``auto_invoked`` is True when the span started as a direct child of
    Semantic Kernel's ``AutoFunctionInvocationLoop`` span, i.e. the model asked
    for the function. Connectors that send no tool call id (Ollama) rely on it.
    """
    operation = attributes.get(OPERATION)
    if operation == "execute_tool":
        # Every KernelFunction.invoke opens an execute_tool span. A function
        # the model asked for carries a tool call id or runs inside the loop.
        if auto_invoked or attributes.get(TOOL_CALL_ID):
            return FiSpanKindValues.TOOL.value
        return FiSpanKindValues.CHAIN.value
    if isinstance(operation, str) and operation in KIND_BY_OPERATION:
        return KIND_BY_OPERATION[operation]
    if name == AUTO_FUNCTION_INVOCATION_SPAN:
        return FiSpanKindValues.CHAIN.value
    # Fallbacks for operation strings this package has not seen.
    if attributes.get(AGENT_NAME):
        return FiSpanKindValues.AGENT.value
    if attributes.get(MODEL):
        return FiSpanKindValues.LLM.value
    if attributes.get(TOOL_NAME):
        return FiSpanKindValues.TOOL.value
    return None


def _mime(value: str) -> str:
    stripped = value.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    ):
        try:
            json.loads(stripped)
            return FiMimeTypeValues.JSON.value
        except ValueError:
            pass
    return FiMimeTypeValues.TEXT.value


def _set_io(mapped: Dict[str, Any], value_key: str, mime_key: str, value: Any) -> None:
    if value is None or value_key in mapped:
        return
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    mapped[value_key] = text
    mapped[mime_key] = _mime(text)


def map_sk_attributes(
    attributes: Mapping[str, Any],
    *,
    name: str = "",
    sensitive: bool = False,
    auto_invoked: bool = False,
    hide_inputs: bool = False,
    hide_outputs: bool = False,
) -> Dict[str, Any]:
    """Return a new attribute dict with Future AGI keys added (see module docstring).

    ``hide_inputs`` / ``hide_outputs`` (TraceConfig) apply when ``sensitive`` is
    True; with ``sensitive`` False every content key is removed anyway.
    """
    mapped = dict(attributes or {})
    kind = kind_for(name, mapped, auto_invoked=auto_invoked)

    if kind is not None:
        mapped.setdefault(FI_SPAN_KIND, kind)
        mapped.setdefault(GEN_AI_SPAN_KIND, kind)

    system = mapped.get(SYSTEM)
    if system and not mapped.get(PROVIDER_NAME):
        mapped[PROVIDER_NAME] = system

    conversation_id = mapped.get(CONVERSATION_ID)
    if conversation_id and not mapped.get(SESSION_ID):
        mapped[SESSION_ID] = str(conversation_id)

    if kind == FiSpanKindValues.LLM.value:
        inp = mapped.get(INPUT_TOKENS)
        out = mapped.get(OUTPUT_TOKENS)
        if isinstance(inp, int) and isinstance(out, int) and TOTAL_TOKENS not in mapped:
            mapped[TOTAL_TOKENS] = inp + out
    else:
        for key in PROMOTED_USAGE_KEYS:
            if key in mapped:
                suffix = key[len("gen_ai.usage."):] if key.startswith("gen_ai.usage.") else key
                mapped.setdefault(USAGE_NAMESPACE + suffix, mapped.pop(key))

    if sensitive:
        if kind == FiSpanKindValues.AGENT.value:
            _set_io(mapped, SpanAttributes.INPUT_VALUE, SpanAttributes.INPUT_MIME_TYPE, mapped.get(INPUT_MESSAGES))
            _set_io(mapped, SpanAttributes.OUTPUT_VALUE, SpanAttributes.OUTPUT_MIME_TYPE, mapped.get(OUTPUT_MESSAGES))
        elif mapped.get(OPERATION) == "execute_tool":
            _set_io(mapped, SpanAttributes.INPUT_VALUE, SpanAttributes.INPUT_MIME_TYPE, mapped.get(TOOL_CALL_ARGUMENTS))
            _set_io(mapped, SpanAttributes.OUTPUT_VALUE, SpanAttributes.OUTPUT_MIME_TYPE, mapped.get(TOOL_CALL_RESULT))
        hidden = (INPUT_CONTENT_KEYS if hide_inputs else ()) + (OUTPUT_CONTENT_KEYS if hide_outputs else ())
        for key in hidden:
            mapped.pop(key, None)
    else:
        for key in CONTENT_KEYS:
            mapped.pop(key, None)

    return mapped


def _context_attributes(parent_context: Optional[Context]) -> Iterator[Tuple[str, Any]]:
    """Yield the ``using_attributes`` values visible from ``parent_context``."""
    for key in CONTEXT_ATTRIBUTES:
        value = get_value(key, parent_context)
        if value is not None:
            yield key, value


def _replace_attributes(span: ReadableSpan, mapped: Dict[str, Any]) -> None:
    """Write ``mapped`` back onto the ended span so later processors see it.

    ``ReadableSpan.attributes`` is a read-only view over ``span._attributes``.
    The SDK hands one span object to every processor in order, so replacing
    ``_attributes`` here is visible to the exporting processor that runs next.
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


def _is_semantic_kernel(span: Any) -> bool:
    scope = getattr(span, "instrumentation_scope", None)
    name = getattr(scope, "name", None) or ""
    return name == SK_SCOPE_PREFIX or name.startswith(SK_SCOPE_PREFIX + ".")


def _is_auto_function_invocation_loop(span: Any) -> bool:
    return getattr(span, "name", None) == AUTO_FUNCTION_INVOCATION_SPAN and _is_semantic_kernel(span)


def _span_key(span: Any) -> Optional[Tuple[int, int]]:
    context = span.get_span_context() if hasattr(span, "get_span_context") else getattr(span, "context", None)
    if context is None:
        return None
    return (context.trace_id, context.span_id)


# Bound on spans that started inside the loop but have not ended yet. A span
# that never ends (a producer bug) must not grow this without limit.
_MAX_PENDING_AUTO_INVOKED = 10_000


class SemanticKernelSpanProcessor(SpanProcessor):
    """Map Semantic Kernel's native spans to Future AGI conventions.

    Install it ahead of the exporting processor (``instrument()`` does this) so
    the exporter sees the mapped attributes. It does not export anything.

    ``config`` is a ``fi_instrumentation.TraceConfig``; when omitted one is
    built from the environment (``FI_HIDE_INPUTS``, ``FI_HIDE_OUTPUTS``). Its
    ``hide_inputs`` / ``hide_outputs`` drop the input / output content keys
    that ``sensitive=True`` would otherwise keep.
    """

    def __init__(self, sensitive: bool = False, config: Optional[TraceConfig] = None) -> None:
        if config is None:
            config = TraceConfig()
        elif not isinstance(config, TraceConfig):
            raise TypeError(
                "config must be a fi_instrumentation.TraceConfig, got {0}".format(type(config).__name__)
            )
        self.sensitive = bool(sensitive)
        self.config = config
        self.hide_inputs = bool(config.hide_inputs)
        self.hide_outputs = bool(config.hide_outputs)
        self._disabled = False
        # Spans that started as direct children of AutoFunctionInvocationLoop.
        # The parent is only visible at start; on_end gets a ReadableSpan copy,
        # so they are keyed by (trace_id, span_id).
        self._auto_invoked: Dict[Tuple[int, int], None] = {}
        self._auto_invoked_lock = threading.Lock()

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        if self._disabled or not _is_semantic_kernel(span):
            return
        try:
            if _is_auto_function_invocation_loop(trace_api.get_current_span(parent_context)):
                key = _span_key(span)
                if key is not None:
                    with self._auto_invoked_lock:
                        if len(self._auto_invoked) >= _MAX_PENDING_AUTO_INVOKED:
                            self._auto_invoked.pop(next(iter(self._auto_invoked)))
                        self._auto_invoked[key] = None
        except Exception:
            logger.debug("traceai-semantic-kernel: parent lookup failed", exc_info=True)
        try:
            existing = span.attributes or {}
            for key, value in _context_attributes(parent_context):
                if key not in existing:
                    span.set_attribute(key, value)
        except Exception:
            logger.debug("traceai-semantic-kernel: context attribute copy failed", exc_info=True)

    def _pop_auto_invoked(self, span: ReadableSpan) -> bool:
        key = _span_key(span)
        if key is None:
            return False
        with self._auto_invoked_lock:
            if key in self._auto_invoked:
                del self._auto_invoked[key]
                return True
            return False

    def on_end(self, span: ReadableSpan) -> None:
        if self._disabled or not _is_semantic_kernel(span):
            return
        try:
            auto_invoked = self._pop_auto_invoked(span)
            attributes = dict(span.attributes or {})
            mapped = map_sk_attributes(
                attributes,
                name=span.name,
                sensitive=self.sensitive,
                auto_invoked=auto_invoked,
                hide_inputs=self.hide_inputs,
                hide_outputs=self.hide_outputs,
            )
            if mapped != attributes:
                _replace_attributes(span, mapped)
            if attributes.get(ERROR_TYPE) and span.status.status_code is StatusCode.UNSET:
                span._status = Status(StatusCode.ERROR, str(attributes[ERROR_TYPE]))  # type: ignore[attr-defined]
        except Exception:
            logger.debug("traceai-semantic-kernel: span mapping failed", exc_info=True)

    def shutdown(self) -> None:
        self._disabled = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
