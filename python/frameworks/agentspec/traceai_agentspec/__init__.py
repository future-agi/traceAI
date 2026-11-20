"""Trace Oracle Open Agent Spec (``pyagentspec``).

``pyagentspec`` makes no model or tool calls itself: a runtime opens a ``Trace`` and emits spans
and events to that trace's ``SpanProcessor`` objects. This instrumentor attaches one processor
to every ``Trace`` and translates what it receives into traceAI spans.
"""
from __future__ import annotations

import json
import logging
import weakref
from typing import Any, Callable, Collection, Dict, List, Optional

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes as A
from opentelemetry import trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, get_value
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.trace import Status, StatusCode
from pyagentspec.tracing.spanprocessor import SpanProcessor
from pyagentspec.tracing.spans import (
    AgentExecutionSpan,
    FlowExecutionSpan,
    LlmGenerationSpan,
    ManagerWorkersExecutionSpan,
    NodeExecutionSpan,
    SwarmExecutionSpan,
    ToolExecutionSpan,
)
from wrapt import wrap_function_wrapper

from .version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_LIMIT = 32768
_OUT = f"{A.GEN_AI_OUTPUT_MESSAGES}.0.message"
_GENERATION_PARAMETERS = (
    ("temperature", A.GEN_AI_REQUEST_TEMPERATURE),
    ("top_p", A.GEN_AI_REQUEST_TOP_P),
    ("max_tokens", A.GEN_AI_REQUEST_MAX_TOKENS),
    ("top_k", A.GEN_AI_REQUEST_TOP_K),
    ("seed", A.GEN_AI_REQUEST_SEED),
    ("frequency_penalty", A.GEN_AI_REQUEST_FREQUENCY_PENALTY),
    ("presence_penalty", A.GEN_AI_REQUEST_PRESENCE_PENALTY),
)

# span class -> (kind, span name prefix, component attribute, operation)
_SPANS = (
    (AgentExecutionSpan, FiSpanKindValues.AGENT, "agent", "agent", "invoke_agent"),
    (SwarmExecutionSpan, FiSpanKindValues.AGENT, "swarm", "swarm", "invoke_agent"),
    (ManagerWorkersExecutionSpan, FiSpanKindValues.AGENT, "managerworkers", "managerworkers", "invoke_agent"),
    (FlowExecutionSpan, FiSpanKindValues.CHAIN, "flow", "flow", None),
    (NodeExecutionSpan, FiSpanKindValues.CHAIN, "node", "node", None),
    (LlmGenerationSpan, FiSpanKindValues.LLM, "llm_config", "llm", "chat"),
    (ToolExecutionSpan, FiSpanKindValues.TOOL, "tool", "tool", "execute_tool"),
)


def _count(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


_PROVIDERS = {"openaicompatible": "openai", "ocigenai": "oci", "azureopenai": "azure.ai.openai", "vllm": "vllm"}


def _provider(llm_config: Any) -> Optional[str]:
    """The configured provider, else one derived from the config class (``OllamaConfig`` -> ollama)."""
    for name in ("provider", "api_provider"):
        value = getattr(llm_config, name, None)
        if isinstance(value, str) and value:
            return value
    stem = type(llm_config).__name__
    stem = (stem[: -len("Config")] if stem.endswith("Config") else stem).lower()
    return _PROVIDERS.get(stem, stem) if stem and stem != "llm" else None


def _json(value: Any) -> str:
    return json.dumps(value, default=str)[:_LIMIT]


def _safe(method: Callable[..., Any]) -> Callable[..., Any]:
    """Tracing must never reach the user's code: pyagentspec re-raises processor errors."""

    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except Exception:
            logger.debug("agentspec tracing failed in %s", method.__name__, exc_info=True)
            return None

    guarded.__name__ = method.__name__
    return guarded


class _State:
    """What is known about one open pyagentspec span."""

    def __init__(self, span: Any, kind: FiSpanKindValues) -> None:
        self.span = span
        self.kind = kind
        self.error_type: Optional[str] = None
        self.chunks: List[str] = []
        self.chunk_length = 0
        self.responded = False
        self.has_output = False


class FIAgentSpecSpanProcessor(SpanProcessor):
    """Translates the spans and events of one ``pyagentspec`` ``Trace`` into traceAI spans."""

    def __init__(self, tracer: Any, config: TraceConfig, mask_sensitive_information: bool = False) -> None:
        super().__init__(mask_sensitive_information=mask_sensitive_information)
        self._tracer = tracer
        self._config = config
        self._open: Dict[str, _State] = {}
        self.active = True
        c = config
        self._capture_input = not (c.hide_inputs or c.hide_input_text or c.hide_input_messages)
        self._capture_output = not (c.hide_outputs or c.hide_output_text or c.hide_output_messages)
        self._capture_io_in = not c.hide_inputs
        self._capture_io_out = not c.hide_outputs

    # -- helpers ---------------------------------------------------------------------------------

    def _sensitive_ok(self, allowed: bool) -> bool:
        return allowed and not self.mask_sensitive_information

    def _payload(self, state: _State, attribute: str, value: Any, allowed: bool) -> None:
        if not self._sensitive_ok(allowed) or value is None:
            return
        state.span.set_attribute(attribute, _json(value))
        state.span.set_attribute(
            A.INPUT_MIME_TYPE if attribute == A.INPUT_VALUE else A.OUTPUT_MIME_TYPE, "application/json"
        )
        if attribute == A.OUTPUT_VALUE:
            state.has_output = True

    def _generation_parameters(self, state: _State, parameters: Any) -> None:
        if parameters is None:
            return
        values = parameters.model_dump() if hasattr(parameters, "model_dump") else dict(parameters)
        for name, attribute in _GENERATION_PARAMETERS:
            number = _number(values.get(name))
            if number is not None:
                state.span.set_attribute(attribute, number)
        stop = values.get("stop")
        if isinstance(stop, str):
            stop = [stop]
        if isinstance(stop, (list, tuple)) and stop and all(isinstance(s, str) for s in stop):
            state.span.set_attribute(A.GEN_AI_REQUEST_STOP_SEQUENCES, list(stop))

    # -- span lifecycle --------------------------------------------------------------------------

    def _start_span(self, span: Any) -> Optional[_State]:
        for cls, kind, component, prefix, operation in _SPANS:
            if isinstance(span, cls):
                break
        else:
            return None  # RootSpan and runtime-specific spans are not translated
        target = getattr(span, component, None)
        attributes: Dict[str, Any] = {}
        if operation:
            attributes[A.GEN_AI_OPERATION_NAME] = operation
        if kind is FiSpanKindValues.LLM:
            label = getattr(target, "model_id", None) or "llm"
            provider = _provider(target)
            attributes[A.GEN_AI_REQUEST_MODEL] = label
            if provider:
                attributes[A.GEN_AI_PROVIDER_NAME] = provider
        else:
            label = getattr(target, "name", None) or prefix
        if kind is FiSpanKindValues.AGENT:
            attributes[A.GEN_AI_AGENT_NAME] = label
            if getattr(target, "id", None):
                attributes[A.GEN_AI_AGENT_ID] = target.id
            if getattr(target, "description", None):
                attributes[A.GEN_AI_AGENT_DESCRIPTION] = target.description
        elif kind is FiSpanKindValues.TOOL:
            attributes[A.GEN_AI_TOOL_NAME] = label
            if getattr(target, "description", None):
                attributes[A.GEN_AI_TOOL_DESCRIPTION] = target.description
        parent = self._nearest_open_ancestor(span)
        context = trace.set_span_in_context(parent.span) if parent is not None else None
        otel_span = self._tracer.start_span(
            f"agentspec.{prefix}.{label}", context=context, attributes=attributes, fi_span_kind=kind
        )
        state = _State(otel_span, kind)
        self._open[span.id] = state
        if kind is FiSpanKindValues.LLM:
            self._generation_parameters(state, getattr(target, "default_generation_parameters", None))
        return state

    def _nearest_open_ancestor(self, span: Any) -> Optional[_State]:
        """The state of the closest ancestor we translated, looking through spans we did not."""
        ancestor = getattr(span, "_parent_span", None)
        for _ in range(256):  # the chain is finite; the bound only guards a malformed cycle
            if ancestor is None:
                return None
            state = self._open.get(getattr(ancestor, "id", None))
            if state is not None:
                return state
            ancestor = getattr(ancestor, "_parent_span", None)
        return None

    @_safe
    def on_start(self, span: Any) -> None:
        if not self.active or get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return
        self._start_span(span)

    async def on_start_async(self, span: Any) -> None:
        self.on_start(span)

    def _finish(self, state: _State, unfinished: bool = False) -> None:
        try:
            if state.kind is FiSpanKindValues.LLM and state.chunks and not state.responded:
                if self._sensitive_ok(self._capture_output):
                    state.span.set_attribute(A.OUTPUT_VALUE, "".join(state.chunks))
                state.span.set_attribute("agentspec.stream.incomplete", True)
            elif state.kind is FiSpanKindValues.TOOL and state.chunks and not state.has_output:
                if self._sensitive_ok(self._capture_output):
                    state.span.set_attribute(A.OUTPUT_VALUE, "".join(state.chunks))
            if unfinished:
                state.span.set_attribute("agentspec.span.unfinished", True)
            if state.error_type:
                # Exception text can contain prompts, payloads or credentials: class name only.
                state.span.set_attribute("error.type", state.error_type)
                state.span.set_status(Status(StatusCode.ERROR))
        except Exception:
            logger.debug("agentspec span finalisation failed", exc_info=True)
        finally:
            state.span.end()

    @_safe
    def on_end(self, span: Any) -> None:
        state = self._open.pop(span.id, None)
        if state is not None:
            self._finish(state)

    async def on_end_async(self, span: Any) -> None:
        self.on_end(span)

    # -- events ----------------------------------------------------------------------------------

    @_safe
    def on_event(self, event: Any, span: Any) -> None:
        state = self._open.get(span.id)
        if state is None:
            return
        handler = getattr(self, f"_on_{type(event).__name__}", None)
        if handler is not None:
            handler(state, event)

    async def on_event_async(self, event: Any, span: Any) -> None:
        self.on_event(event, span)

    def _on_ExceptionRaised(self, state: _State, event: Any) -> None:
        state.error_type = str(event.exception_type or "Exception")

    def _inputs(self, state: _State, event: Any) -> None:
        self._payload(state, A.INPUT_VALUE, event.inputs, self._capture_io_in)

    def _outputs(self, state: _State, event: Any) -> None:
        self._payload(state, A.OUTPUT_VALUE, event.outputs, self._capture_io_out)

    _on_AgentExecutionStart = _on_FlowExecutionStart = _on_NodeExecutionStart = _inputs
    _on_AgentExecutionEnd = _outputs

    def _on_FlowExecutionEnd(self, state: _State, event: Any) -> None:
        self._outputs(state, event)
        state.span.set_attribute("agentspec.flow.branch_selected", str(event.branch_selected))

    def _on_NodeExecutionEnd(self, state: _State, event: Any) -> None:
        self._outputs(state, event)
        state.span.set_attribute("agentspec.node.branch_selected", str(event.branch_selected))

    def _on_ToolExecutionRequest(self, state: _State, event: Any) -> None:
        state.span.set_attribute(A.GEN_AI_TOOL_CALL_ID, str(event.request_id))
        self._inputs(state, event)

    def _on_ToolExecutionResponse(self, state: _State, event: Any) -> None:
        state.span.set_attribute(A.GEN_AI_TOOL_CALL_ID, str(event.request_id))
        self._outputs(state, event)

    def _on_ToolExecutionStreamingChunkReceived(self, state: _State, event: Any) -> None:
        self._chunk(state, event.content)

    def _chunk(self, state: _State, text: Any) -> None:
        if isinstance(text, str) and text and state.chunk_length < _LIMIT:
            text = text[: _LIMIT - state.chunk_length]
            state.chunks.append(text)
            state.chunk_length += len(text)

    def _on_LlmGenerationRequest(self, state: _State, event: Any) -> None:
        self._generation_parameters(state, event.llm_generation_config)
        if self._sensitive_ok(self._capture_input):
            messages = [{"role": m.role, "content": m.content} for m in event.prompt or []]
            if messages:
                state.span.set_attribute(A.INPUT_VALUE, _json(messages))
                state.span.set_attribute(A.INPUT_MIME_TYPE, "application/json")

    def _on_LlmGenerationChunkReceived(self, state: _State, event: Any) -> None:
        self._chunk(state, event.content)

    def _on_LlmGenerationResponse(self, state: _State, event: Any) -> None:
        state.responded = True
        span = state.span
        if event.completion_id:
            span.set_attribute(A.GEN_AI_RESPONSE_ID, str(event.completion_id))
        usage_in, usage_out = _count(event.input_tokens), _count(event.output_tokens)
        for attribute, value in (
            (A.GEN_AI_USAGE_INPUT_TOKENS, usage_in),
            (A.GEN_AI_USAGE_OUTPUT_TOKENS, usage_out),
            (A.GEN_AI_USAGE_TOTAL_TOKENS, usage_in + usage_out if None not in (usage_in, usage_out) else None),
        ):
            if value is not None:
                span.set_attribute(attribute, value)
        sensitive = self._sensitive_ok(self._capture_output)
        if sensitive and isinstance(event.content, str) and event.content:
            span.set_attribute(A.OUTPUT_VALUE, event.content[:_LIMIT])
        for position, call in enumerate(event.tool_calls or []):
            prefix = f"{_OUT}.tool_calls.{position}.tool_call"
            span.set_attribute(f"{prefix}.id", str(call.call_id))
            span.set_attribute(f"{prefix}.function.name", str(call.tool_name))
            if sensitive and call.arguments:
                span.set_attribute(f"{prefix}.function.arguments", str(call.arguments)[:_LIMIT])

    # -- trace lifecycle -------------------------------------------------------------------------

    @_safe
    def startup(self) -> None:
        return None

    async def startup_async(self) -> None:
        return None

    @_safe
    def shutdown(self) -> None:
        # Spans a runtime never ended must not leak: end the innermost first.
        for key in reversed(list(self._open)):
            self._finish(self._open.pop(key), unfinished=True)

    async def shutdown_async(self) -> None:
        self.shutdown()


class AgentSpecInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Attach a traceAI span processor to every ``pyagentspec`` ``Trace``."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("pyagentspec >= 26.1.0, < 27",)

    def _instrument(self, **kwargs: Any) -> None:
        from pyagentspec.tracing.trace import Trace

        config = kwargs.get("config") or TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        tracer = FITracer(trace.get_tracer(__name__, __version__, kwargs.get("tracer_provider")), config)
        mask = bool(kwargs.get("mask_sensitive_information", False))
        # Weak: a Trace owns its processor (through span_processors), so a closed Trace frees it.
        # Holding them strongly here would keep one processor per request alive forever.
        self._processors: "weakref.WeakSet[FIAgentSpecSpanProcessor]" = weakref.WeakSet()
        self._original = Trace.__dict__["__init__"]

        def wrapped(wrapped: Any, instance: Any, args: Any, kw: Any) -> None:
            wrapped(*args, **kw)
            try:
                if not any(isinstance(p, FIAgentSpecSpanProcessor) for p in instance.span_processors):
                    processor = FIAgentSpecSpanProcessor(tracer, config, mask)
                    self._processors.add(processor)
                    # a new list: the one the caller passed in is theirs
                    instance.span_processors = [*instance.span_processors, processor]
            except Exception:
                logger.debug("agentspec processor attachment failed", exc_info=True)

        wrap_function_wrapper("pyagentspec.tracing.trace", "Trace.__init__", wrapped)

    def _uninstrument(self, **kwargs: Any) -> None:
        from pyagentspec.tracing.trace import Trace

        for processor in getattr(self, "_processors", []):
            processor.active = False  # traces that are still running stop translating
        self._processors = weakref.WeakSet()
        original = getattr(self, "_original", None)
        if original is not None:
            Trace.__init__ = original
            self._original = None


__all__ = ["AgentSpecInstrumentor", "FIAgentSpecSpanProcessor", "__version__"]
