"""Trace Microsoft Prompt flow (``promptflow-tracing`` / ``promptflow-core``).

Prompt flow already opens an OpenTelemetry span for every ``@trace`` function, every DAG node
and tool, and every flow run (flex and DAG), but it does so on the global tracer provider, with
its own attribute schema, with the raw inputs and outputs, and with exception messages. This
instrumentor re-routes those spans onto a traceAI tracer instead:

* the flow run is a ``CHAIN`` span, nodes and tools are child spans (``TOOL``; LLM calls made
  through Prompt flow's OpenAI integration are ``LLM`` spans with model and token usage);
* inputs and outputs are exported as ``input.value`` / ``output.value`` under the ``TraceConfig``
  hide rules, and Prompt flow's own ``inputs`` / ``output`` / prompt attributes and events are
  not exported;
* generator results end their span on exhaustion, error, ``close()`` or when dropped;
* errors are ``ERROR`` with ``error.type``; the exception is re-raised unchanged and its
  message is never exported;
* a failure inside the instrumentation never reaches the user's call.

Prompt flow ships with tracing switched off (``PF_DISABLE_TRACING`` defaults to ``true``), so
``instrument()`` turns it on unless the variable is already set, and restores it afterwards.
"""
from __future__ import annotations

import contextlib
import contextvars
import importlib
import logging
import os
import sys
from typing import Any, Callable, Collection, Dict, List, Optional, Tuple

import wrapt
from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import SpanAttributes as A
from opentelemetry import trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY  # noqa: F401  (documented behaviour)
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.trace import INVALID_SPAN, Status, StatusCode

from .version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_PROVIDER = "promptflow"
_LIMIT = 32768
_ENV = "PF_DISABLE_TRACING"
_INCOMPLETE = "promptflow.stream.incomplete"

# promptflow span_type -> traceAI span kind. ``Function`` is a tool unless it is outermost.
_KINDS = {
    "Flow": "CHAIN",
    "LangChain": "CHAIN",
    "LLM": "LLM",
    "Embedding": "EMBEDDING",
    "Retrieval": "RETRIEVER",
    "Function": "TOOL",
}

# Prompt flow's own content attributes and events. They carry raw inputs, outputs, prompt templates
# and variables, so they never reach a traceAI span, whichever code path writes them (a hook this
# instrumentor replaced, a binding it missed, or a Prompt flow version with new hooks).
_NATIVE_ATTRIBUTES = frozenset({
    "inputs", "output", "llm.generated_message", "embedding.embeddings", "prompt.template", "prompt.variables",
})
_NATIVE_EVENT_PREFIX = "promptflow."


def _native_attribute(key: Any) -> bool:
    return isinstance(key, str) and (key in _NATIVE_ATTRIBUTES or key.startswith("prompt."))


# How many promptflow spans enclose the span being started (0 = outermost).
_DEPTH: "contextvars.ContextVar[int]" = contextvars.ContextVar("traceai_promptflow_depth", default=0)

# Module attributes replaced while instrumented: (module, name) -> original.
_state: Dict[str, Any] = {"tracer": None, "config": None}


def _safe(function: Callable[..., Any]) -> Callable[..., Any]:
    """Run a recording hook; whatever it does, it must not break the user's call."""

    def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except Exception:
            logger.debug("promptflow instrumentation hook failed", exc_info=True)
            return None

    guarded.__name__ = function.__name__
    return guarded


def _serialize(value: Any) -> Optional[str]:
    from promptflow.tracing import _trace

    text = _trace.serialize_attribute(value)
    return text[:_LIMIT] if isinstance(text, str) else None


class _Span(wrapt.ObjectProxy):  # type: ignore[misc]
    """The traceAI span handed to promptflow; maps its ``span_type`` onto the span kind and drops
    Prompt flow's native content attributes, events, exception messages and status descriptions."""

    def __init__(self, wrapped: Any, outermost: bool) -> None:
        super().__init__(wrapped)
        self._self_outermost = outermost
        self._self_span_type: Optional[str] = None

    def __enter__(self) -> "_Span":
        return self

    def _kind(self, attributes: Any) -> None:
        span_type = attributes.get("span_type") if hasattr(attributes, "get") else None
        kind = _KINDS.get(span_type) if isinstance(span_type, str) else None
        if kind is None:
            return
        self._self_span_type = span_type
        if span_type == "Function" and self._self_outermost:
            kind = "CHAIN"
        self.__wrapped__.set_attribute(A.GEN_AI_SPAN_KIND, kind)

    def set_attribute(self, key: str, value: Any) -> None:
        if _native_attribute(key):
            return
        self.__wrapped__.set_attribute(key, value)
        if key == "span_type":
            self._kind({key: value})

    def set_attributes(self, attributes: Any) -> None:
        attributes = _without_native(attributes)
        self.__wrapped__.set_attributes(attributes)
        self._kind(attributes)

    def add_event(self, name: str, *args: Any, **kwargs: Any) -> None:
        if isinstance(name, str) and name.startswith(_NATIVE_EVENT_PREFIX):
            return
        self.__wrapped__.add_event(name, *args, **kwargs)

    def record_exception(self, exception: BaseException, *args: Any, **kwargs: Any) -> None:
        _fail(self.__wrapped__, exception)  # class name only: the message and traceback are never exported

    def set_status(self, status: Any, description: Optional[str] = None) -> None:
        code = getattr(status, "status_code", status)
        self.__wrapped__.set_status(Status(code) if isinstance(code, StatusCode) else status)


def _without_native(attributes: Any) -> Any:
    if not hasattr(attributes, "items"):
        return attributes
    return {k: v for k, v in attributes.items() if not _native_attribute(k)}


def _fail(span: Any, exc: BaseException) -> None:
    # Exception text can carry user data or credentials: class name only, no event, no description.
    span.set_attribute(A.ERROR_TYPE, type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR))


@contextlib.contextmanager
def _start_as_current_span(tracer: Any, name: str, context: Any = None, kind: Any = trace.SpanKind.INTERNAL,
                           attributes: Any = None, links: Any = (), start_time: Optional[int] = None,
                           record_exception: bool = True, set_status_on_exception: bool = True) -> Any:
    """Replaces promptflow's ``start_as_current_span``: the span is a traceAI span and errors
    are recorded without their message. The ``tracer`` promptflow passes is ignored."""
    fi_tracer = _state["tracer"]
    span: Any = None
    token = None
    try:
        raw = fi_tracer.start_span(
            name, context=context, kind=kind,
            attributes=dict(_without_native(attributes or {}), **{A.GEN_AI_PROVIDER_NAME: _PROVIDER}),
            links=links, start_time=start_time, record_exception=False, set_status_on_exception=False,
        )
        depth = _DEPTH.get()
        span = _Span(raw, outermost=depth == 0)
        token = _DEPTH.set(depth + 1)
    except Exception:
        logger.debug("promptflow span start failed", exc_info=True)
        span = INVALID_SPAN
    setattr(span, "__should_end", True)
    try:
        if span is INVALID_SPAN:
            yield span  # span creation failed: keep the ambient span as the parent of nested ones
        else:
            with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                yield span
    except GeneratorExit:
        raise
    except BaseException as exc:
        # Includes KeyboardInterrupt and asyncio.CancelledError, which promptflow also records.
        try:
            _fail(span, exc)
        except Exception:
            logger.debug("promptflow error recording failed", exc_info=True)
        raise
    finally:
        if token is not None:
            try:
                _DEPTH.reset(token)
            except ValueError:  # reset from another context (async generator teardown)
                pass
        if getattr(span, "__should_end", False) and span.is_recording():
            span.end()


def _handle_span_exception(span: Any, exception: BaseException) -> None:
    """Replaces promptflow's: it recorded the exception message. Also ends the span of a failed
    stream, which promptflow leaves open."""
    try:
        if span.is_recording():
            _fail(span, exception)
            if not getattr(span, "__should_end", True):
                span.end()
    except Exception:
        logger.debug("promptflow error recording failed", exc_info=True)


_REQUEST_PARAMETERS = (
    ("temperature", A.GEN_AI_REQUEST_TEMPERATURE),
    ("top_p", A.GEN_AI_REQUEST_TOP_P),
    ("max_tokens", A.GEN_AI_REQUEST_MAX_TOKENS),
    ("max_completion_tokens", A.GEN_AI_REQUEST_MAX_TOKENS),
    ("presence_penalty", A.GEN_AI_REQUEST_PRESENCE_PENALTY),
    ("frequency_penalty", A.GEN_AI_REQUEST_FREQUENCY_PENALTY),
    ("seed", A.GEN_AI_REQUEST_SEED),
)


def _request_parameters(span: Any, inputs: Any) -> None:
    """Request model and sampling parameters of an LLM or embedding call (never message content)."""
    if getattr(span, "_self_span_type", None) not in ("LLM", "Embedding") or not isinstance(inputs, dict):
        return
    for name in ("model", "engine", "deployment_id"):
        if isinstance(inputs.get(name), str) and inputs[name]:
            span.set_attribute(A.GEN_AI_REQUEST_MODEL, inputs[name])
            break
    for name, attribute in _REQUEST_PARAMETERS:
        value = inputs.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            span.set_attribute(attribute, value)
    stop = inputs.get("stop")
    stop = [stop] if isinstance(stop, str) else stop
    if isinstance(stop, (list, tuple)) and stop and all(isinstance(item, str) for item in stop):
        span.set_attribute(A.GEN_AI_REQUEST_STOP_SEQUENCES, list(stop))


def _enrich_span_with_input(span: Any, input: Any) -> Any:
    try:
        _request_parameters(span, input)
        if not _state["config"].hide_inputs:
            text = _serialize(input)
            if text is not None:
                span.set_attribute(A.INPUT_VALUE, text)
                span.set_attribute(A.INPUT_MIME_TYPE, "application/json")
    except Exception:
        logger.debug("promptflow input capture failed", exc_info=True)
    return input


def _is_embedding_response(output: Any) -> bool:
    data = getattr(output, "data", None)
    return isinstance(data, list) and bool(data) and all(hasattr(item, "embedding") for item in data)


@_safe
def _enrich_span_with_output(span: Any, output: Any) -> None:
    if _state["config"].hide_outputs or _is_embedding_response(output):
        return  # embedding vectors are not exported
    text = _serialize(output)
    if text is not None:
        span.set_attribute(A.OUTPUT_VALUE, text)
        span.set_attribute(A.OUTPUT_MIME_TYPE, "application/json")


@_safe
def _enrich_span_with_llm(span: Any, model: Any, generated_message: Any) -> None:
    if isinstance(model, str) and model:
        span.set_attribute(A.GEN_AI_RESPONSE_MODEL, model)


@_safe
def _enrich_span_with_embedding(span: Any, inputs: Any, output: Any) -> None:
    model = getattr(output, "model", None)
    if isinstance(model, str) and model:
        span.set_attribute(A.GEN_AI_RESPONSE_MODEL, model)


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@_safe
def _enrich_span_with_openai_tokens(span: Any, trace_type: Any) -> None:
    from promptflow.tracing import _trace

    if getattr(trace_type, "value", trace_type) not in ("LLM", "Embedding"):
        return
    tokens = _trace.token_collector.try_get_openai_tokens(span.get_span_context().span_id)
    if not isinstance(tokens, dict):
        return
    for attribute, names in (
        (A.GEN_AI_USAGE_INPUT_TOKENS, ("prompt_tokens", "input_tokens")),
        (A.GEN_AI_USAGE_OUTPUT_TOKENS, ("completion_tokens", "output_tokens")),
        (A.GEN_AI_USAGE_TOTAL_TOKENS, ("total_tokens",)),
    ):
        for name in names:
            number = _count(tokens.get(name))
            if number is not None:
                span.set_attribute(attribute, number)
                break


@_safe
def _enrich_span_with_trace(span: Any, trace_: Any) -> None:
    function = getattr(trace_, "function", "") or getattr(trace_, "name", "")
    if isinstance(function, str) and function:
        span.set_attribute(A.GEN_AI_TOOL_NAME, function)


def _noop(*args: Any, **kwargs: Any) -> None:
    """Prompt template text and variables are not exported."""


def _finish_early(span: Any) -> None:
    try:
        if span.is_recording():
            span.set_attribute(_INCOMPLETE, True)
            span.end()
    except Exception:
        logger.debug("promptflow stream finalisation failed", exc_info=True)


def _stream_classes(module: Any) -> Tuple[Any, Any]:
    """Subclass promptflow's iterator proxies so a stream also ends on close or when dropped."""

    class TracedIterator(module.TracedIterator):  # type: ignore[misc]
        def close(self) -> Any:
            try:
                closer = getattr(self._iterator, "close", None)
                return closer() if callable(closer) else None
            finally:
                _finish_early(self._span)

        def __exit__(self, *exc_info: Any) -> Any:
            try:
                return super().__exit__(*exc_info)
            finally:
                _finish_early(self._span)

        def __del__(self) -> None:
            _finish_early(self.__dict__.get("_span"))

    class TracedAsyncIterator(module.TracedAsyncIterator):  # type: ignore[misc]
        async def aclose(self) -> Any:
            try:
                closer = getattr(self._iterator, "aclose", None)
                return await closer() if callable(closer) else None
            finally:
                _finish_early(self._span)

        async def __aexit__(self, *exc_info: Any) -> Any:
            try:
                return await super().__aexit__(*exc_info)
            finally:
                _finish_early(self._span)

        def __del__(self) -> None:
            _finish_early(self.__dict__.get("_span"))

    return TracedIterator, TracedAsyncIterator


def _replacements(module: Any) -> Dict[str, Any]:
    traced_iterator, traced_async_iterator = _stream_classes(module)
    return {
        "start_as_current_span": _start_as_current_span,
        "handle_span_exception": _handle_span_exception,
        "enrich_span_with_input": _enrich_span_with_input,
        "enrich_span_with_output": _enrich_span_with_output,
        "enrich_span_with_llm": _enrich_span_with_llm,
        "enrich_span_with_embedding": _enrich_span_with_embedding,
        "enrich_span_with_openai_tokens": _enrich_span_with_openai_tokens,
        "enrich_span_with_trace": _enrich_span_with_trace_chained,
        "enrich_span_with_prompt_info": _noop,
        "enrich_prompt_template": _noop,
        "TracedIterator": traced_iterator,
        "TracedAsyncIterator": traced_async_iterator,
    }


def _enrich_span_with_trace_chained(span: Any, trace_: Any) -> None:
    # Keep promptflow's own structural attributes (span_type, function, node_name, run context)
    # and add the tool name on top; they carry no user content.
    original = (_state.get("originals") or {}).get(("promptflow.tracing._trace", "enrich_span_with_trace"))
    if original is not None:
        original(span, trace_)
    _enrich_span_with_trace(span, trace_)


# Modules imported on request, in case they are not loaded yet: ``_experimental`` re-exports
# ``enrich_prompt_template`` and the flow executor binds two helpers by name.
_TARGETS = (
    ("promptflow.tracing._experimental", ("enrich_prompt_template",)),
    ("promptflow.executor.flow_executor", ("start_as_current_span", "enrich_span_with_input")),
)

# Hooks that write native content onto a span, and the one that routes spans to traceAI.
_CONTENT_HOOKS = (
    "enrich_span_with_input", "enrich_span_with_output", "enrich_span_with_llm", "enrich_span_with_embedding",
    "enrich_span_with_prompt_info", "enrich_prompt_template", "handle_span_exception", "TracedIterator",
)
_SPAN_HOOK = "start_as_current_span"


def _bound_modules(name: str, original: Any) -> List[Any]:
    """Every loaded ``promptflow`` module that bound ``original`` under ``name`` at import time
    (``from ..._trace import name``). Those bindings never see a patch of ``_trace`` itself."""
    return [
        module for module_name, module in list(sys.modules.items())
        if module is not None and module_name.startswith("promptflow") and getattr(module, name, None) is original
    ]


class PromptflowInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Instrument Prompt flow flow runs, nodes, tools and LLM calls."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("promptflow-tracing >= 1.12.0, < 2",)

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config") or TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        base = importlib.import_module("promptflow.tracing._trace")
        if not hasattr(base, _SPAN_HOOK):
            # Without it Prompt flow's spans cannot be routed to traceAI: refuse rather than leave
            # its native content hooks live on spans we do not control.
            logger.warning(
                "traceai-promptflow: promptflow.tracing._trace.%s is missing in this Prompt flow version; "
                "not instrumenting", _SPAN_HOOK,
            )
            self._set_env = False
            return
        for name in _CONTENT_HOOKS:
            if not hasattr(base, name):
                # Native content stays off our spans regardless: the span drops those attributes and events.
                logger.warning(
                    "traceai-promptflow: promptflow.tracing._trace.%s is missing in this Prompt flow version; "
                    "native content is still masked on traceAI spans", name,
                )
        _state["tracer"] = FITracer(trace.get_tracer(__name__, __version__, kwargs.get("tracer_provider")), config)
        _state["config"] = config
        originals: Dict[Tuple[str, str], Any] = {}
        _state["originals"] = originals
        replacements = _replacements(base)
        for module_name, _names in _TARGETS:
            try:
                importlib.import_module(module_name)
            except ImportError:
                pass  # e.g. promptflow-core (the executor) is not installed
        for name in replacements:
            original = getattr(base, name, None)
            if original is None:
                continue
            for module in _bound_modules(name, original):
                originals[(module.__name__, name)] = original
        for (module_name, name) in list(originals):
            setattr(sys.modules[module_name], name, replacements[name])
        self._set_env = _ENV not in os.environ
        if self._set_env:
            os.environ[_ENV] = "false"

    def _uninstrument(self, **kwargs: Any) -> None:
        for (module_name, name), original in (_state.get("originals") or {}).items():
            setattr(sys.modules[module_name], name, original)
        _state["originals"] = {}
        _state["tracer"] = _state["config"] = None
        if getattr(self, "_set_env", False):
            os.environ.pop(_ENV, None)
            self._set_env = False


__all__ = ["PromptflowInstrumentor", "__version__"]
