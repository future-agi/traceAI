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
    """The traceAI span handed to promptflow; maps its ``span_type`` onto the span kind."""

    def __init__(self, wrapped: Any, outermost: bool) -> None:
        super().__init__(wrapped)
        self._self_outermost = outermost

    def __enter__(self) -> "_Span":
        return self

    def _kind(self, attributes: Any) -> None:
        span_type = attributes.get("span_type") if hasattr(attributes, "get") else None
        kind = _KINDS.get(span_type) if isinstance(span_type, str) else None
        if kind is None:
            return
        if span_type == "Function" and self._self_outermost:
            kind = "CHAIN"
        self.__wrapped__.set_attribute(A.GEN_AI_SPAN_KIND, kind)

    def set_attribute(self, key: str, value: Any) -> None:
        self.__wrapped__.set_attribute(key, value)
        if key == "span_type":
            self._kind({key: value})

    def set_attributes(self, attributes: Any) -> None:
        self.__wrapped__.set_attributes(attributes)
        self._kind(attributes)


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
            name, context=context, kind=kind, attributes=dict(attributes or {}, **{A.GEN_AI_PROVIDER_NAME: _PROVIDER}),
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


def _enrich_span_with_input(span: Any, input: Any) -> Any:
    try:
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


# Modules that bind promptflow's helpers by name, and which of them each rebinds.
_TARGETS = (
    ("promptflow.tracing._trace", None),
    ("promptflow.tracing._experimental", ("enrich_prompt_template",)),
    ("promptflow.executor.flow_executor", ("start_as_current_span", "enrich_span_with_input")),
)


class PromptflowInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Instrument Prompt flow flow runs, nodes, tools and LLM calls."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("promptflow-tracing >= 1.12.0, < 2",)

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config") or TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        _state["tracer"] = FITracer(trace.get_tracer(__name__, __version__, kwargs.get("tracer_provider")), config)
        _state["config"] = config
        originals: Dict[Tuple[str, str], Any] = {}
        _state["originals"] = originals
        base = importlib.import_module("promptflow.tracing._trace")
        replacements = _replacements(base)
        for module_name, names in _TARGETS:
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue  # e.g. promptflow-core (the executor) is not installed
            for name in names or tuple(replacements):
                if hasattr(module, name) and name in replacements:
                    originals[(module_name, name)] = getattr(module, name)
        for (module_name, name) in list(originals):
            setattr(importlib.import_module(module_name), name, replacements[name])
        self._set_env = _ENV not in os.environ
        if self._set_env:
            os.environ[_ENV] = "false"

    def _uninstrument(self, **kwargs: Any) -> None:
        for (module_name, name), original in (_state.get("originals") or {}).items():
            setattr(importlib.import_module(module_name), name, original)
        _state["originals"] = {}
        _state["tracer"] = _state["config"] = None
        if getattr(self, "_set_env", False):
            os.environ.pop(_ENV, None)
            self._set_env = False


__all__ = ["PromptflowInstrumentor", "__version__"]
