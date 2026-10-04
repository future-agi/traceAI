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
  for every kernel function invocation. Only functions the model asked for
  (auto function invocation) carry ``gen_ai.tool.call.id``
  (``kernel.py`` line 471 passes the function call content as metadata).
* ``AutoFunctionInvocationLoop``: ``connectors/ai/chat_completion_client_base.py``
  lines 137 and 410-424 (``sk.available_functions`` only).

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
  copied to ``input.value`` / ``output.value``.
* Status ``ERROR`` when ``error.type`` is set but the status was left unset.
  An existing status is never cleared or downgraded.

The processor never raises into the OpenTelemetry SDK.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from opentelemetry.context import Context, get_value
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import Status, StatusCode

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
    # "execute_tool" is resolved in kind_for(): TOOL with a tool call id, else CHAIN.
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


def kind_for(name: str, attributes: Mapping[str, Any]) -> Optional[str]:
    """Return the Future AGI span kind for a Semantic Kernel span, or None."""
    operation = attributes.get(OPERATION)
    if operation == "execute_tool":
        # Every KernelFunction.invoke opens an execute_tool span. Only a
        # function the model asked for carries a tool call id.
        if attributes.get(TOOL_CALL_ID):
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
) -> Dict[str, Any]:
    """Return a new attribute dict with Future AGI keys added (see module docstring)."""
    mapped = dict(attributes or {})
    kind = kind_for(name, mapped)

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


class SemanticKernelSpanProcessor(SpanProcessor):
    """Map Semantic Kernel's native spans to Future AGI conventions.

    Install it ahead of the exporting processor (``instrument()`` does this) so
    the exporter sees the mapped attributes. It does not export anything.
    """

    def __init__(self, sensitive: bool = False) -> None:
        self.sensitive = bool(sensitive)
        self._disabled = False

    def on_start(self, span: Span, parent_context: Optional[Context] = None) -> None:
        if self._disabled or not _is_semantic_kernel(span):
            return
        try:
            existing = span.attributes or {}
            for key, value in _context_attributes(parent_context):
                if key not in existing:
                    span.set_attribute(key, value)
        except Exception:
            logger.debug("traceai-semantic-kernel: context attribute copy failed", exc_info=True)

    def on_end(self, span: ReadableSpan) -> None:
        if self._disabled or not _is_semantic_kernel(span):
            return
        try:
            attributes = dict(span.attributes or {})
            mapped = map_sk_attributes(attributes, name=span.name, sensitive=self.sensitive)
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
