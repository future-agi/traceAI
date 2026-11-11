"""Wrappers for SDK execution boundaries, independent of tool registration."""

import functools
import inspect
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

from opentelemetry.trace import SpanKind, Status, StatusCode, use_span

from ._agent_wrapper import (
    MODEL_REQUEST_SPAN_NAME,
    TOOL_CALL_SPAN_NAME,
    _ACTIVE_USAGE,
    _parent_run_context,
    _record_usage,
    _usage_values,
    safe_serialize,
)
from ._attributes import PydanticAIAttributes as A, PydanticAISpanKind


_ACTIVE_MODEL_REQUEST = ContextVar("pydantic_ai_model_request", default=None)


def _serialize(value):
    # Observing arguments/results must not turn a successful call into a failure.
    try:
        return safe_serialize(value)
    except Exception:
        return "<unavailable>"


def _record_model_usage(span, response):
    if span is None:
        return
    # Historical Model.request returns (ModelResponse, Usage); modern responses
    # carry RequestUsage, and StreamedResponse exposes usage(). Never replace it.
    try:
        usage = response[1] if isinstance(response, tuple) else getattr(response, "usage", None)
        if callable(usage):
            usage = usage()
        _record_usage(span, _usage_values(usage))
    except Exception:
        # Usage is observation only; retain the SDK's original response.
        pass


@contextmanager
def _model_request_span(model, tracer, current=True):
    operation = _ACTIVE_USAGE.get()
    active = _ACTIVE_MODEL_REQUEST.get()
    # A subclass may delegate to super().request(). Do not double count that,
    # but retain genuinely nested agent runs that happen to share a model.
    if active is not None and active[0] is model and active[1] is operation:
        yield None
        return

    attributes = {A.SPAN_KIND: PydanticAISpanKind.MODEL_REQUEST.value}
    for field, attribute in (("model_name", A.MODEL_NAME), ("system", A.MODEL_PROVIDER)):
        try:
            value = getattr(model, field, None)
        except Exception:
            continue
        if isinstance(value, str):
            attributes[attribute] = value
    options = dict(kind=SpanKind.CLIENT, attributes=attributes, context=_parent_run_context())
    scope = (
        tracer.start_as_current_span(
            MODEL_REQUEST_SPAN_NAME, **options,
            record_exception=False, set_status_on_exception=False,
        ) if current else tracer.start_span(MODEL_REQUEST_SPAN_NAME, **options)
    )
    with scope as span:
        token = _ACTIVE_MODEL_REQUEST.set((model, operation)) if current else None
        start = time.perf_counter()
        try:
            yield span
        except Exception as exc:
            span.set_attribute(A.IS_ERROR, True)
            span.set_attribute(A.ERROR_TYPE, type(exc).__name__)
            span.set_attribute(A.ERROR_MESSAGE, str(exc))
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
        else:
            span.set_attribute(A.IS_ERROR, False)
            span.set_status(Status(StatusCode.OK))
        finally:
            if token is not None:
                _ACTIVE_MODEL_REQUEST.reset(token)
            span.set_attribute(A.DURATION_MS, (time.perf_counter() - start) * 1000)


class _ModelStreamContext:
    """Scope context tokens to each awaited operation, never across a yield.

    Some SDK stream consumers enter and exit their manager in different tasks.
    The span may outlive either task, but a ContextVar token may not cross them.
    """

    def __init__(self, manager, model, span):
        self.manager = manager
        self.model = model
        self.span = span

    @contextmanager
    def _current(self):
        if self.span is None:
            yield
            return
        token = _ACTIVE_MODEL_REQUEST.set((self.model, _ACTIVE_USAGE.get()))
        try:
            with use_span(self.span, end_on_exit=False, record_exception=False,
                          set_status_on_exception=False):
                yield
        finally:
            _ACTIVE_MODEL_REQUEST.reset(token)

    async def __aenter__(self):
        with self._current():
            return await self.manager.__aenter__()

    async def __aexit__(self, exc_type, exc_value, traceback):
        with self._current():
            return await self.manager.__aexit__(exc_type, exc_value, traceback)


def _request_messages(args, kwargs):
    return args[0] if args else kwargs.get("messages")


def _capture(media, method, span, *values):
    # Media observation must never affect the call or log source-bearing data.
    if media is None or span is None:
        return
    try:
        getattr(media, method)(span, *values)
    except Exception:
        pass


def wrap_model_request(original, tracer, streaming=False, media=None):
    """``media`` is the instrumentor's attachment binding (``PydanticAIMedia``) or None."""
    if streaming:
        @functools.wraps(original)
        @asynccontextmanager
        async def stream_wrapper(self, *args, **kwargs):
            with _model_request_span(self, tracer, current=False) as span:
                # Input only: streamed output is not captured.
                _capture(media, "request", span, _request_messages(args, kwargs))
                manager = original(self, *args, **kwargs)
                async with _ModelStreamContext(manager, self, span) as response:
                    yield response
                _record_model_usage(span, response)
        return stream_wrapper

    @functools.wraps(original)
    async def wrapper(self, *args, **kwargs):
        with _model_request_span(self, tracer) as span:
            # Before the provider call, so a provider instrumentor sharing the manager reuses it.
            _capture(media, "request", span, _request_messages(args, kwargs))
            response = await original(self, *args, **kwargs)
            _record_model_usage(span, response)
            # Only a successful response: an error never reaches this line.
            _capture(media, "response", span, response)
            return response
    return wrapper


def wrap_tool_call(original, tracer, legacy=False, media=None):
    signature = inspect.signature(original)

    @functools.wraps(original)
    async def wrapper(self, *args, **kwargs):
        try:
            bound = signature.bind(self, *args, **kwargs).arguments
        except TypeError:
            # Let the SDK, not the observer, reject invalid call arguments.
            return await original(self, *args, **kwargs)
        if legacy:
            name = self.name
            description = getattr(self, "description", None)
            arguments = getattr(bound.get("message"), "args", None)
            retry = getattr(self, "current_retry", None)
            tool_call_id = getattr(bound.get("message"), "tool_call_id", None)
        else:
            name = bound.get("name", "unknown")
            tool = bound.get("tool")
            definition = getattr(tool, "tool_def", tool)
            description = getattr(definition, "description", None)
            arguments = bound.get("tool_args")
            retry = getattr(bound.get("ctx"), "retry", None)
            tool_call_id = getattr(bound.get("ctx"), "tool_call_id", None)
        attributes = {
            A.SPAN_KIND: PydanticAISpanKind.TOOL_CALL.value,
            A.GEN_AI_TOOL_NAME: name,
            A.TOOL_ARGS: _serialize(arguments),
        }
        if isinstance(description, str):
            attributes[A.GEN_AI_TOOL_DESCRIPTION] = description[:500]
        if isinstance(retry, int):
            attributes[A.TOOL_RETRY_COUNT] = retry
        with tracer.start_as_current_span(
            f"{TOOL_CALL_SPAN_NAME}.{name}", kind=SpanKind.INTERNAL,
            attributes=attributes, context=_parent_run_context(),
            record_exception=False, set_status_on_exception=False,
        ) as span:
            start = time.perf_counter()
            try:
                result = await original(self, *args, **kwargs)
            except Exception as exc:
                # Modern toolsets let ModelRetry reach the SDK's caller. Observe
                # the attempt, but do not swallow, replace or re-run it.
                span.set_attribute(A.TOOL_IS_ERROR, True)
                span.set_attribute(A.TOOL_ERROR_MESSAGE, str(exc))
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                raise
            else:
                # Historical Tool.run catches ModelRetry itself and returns a
                # RetryPromptPart. It is a failed attempt, not a failed run.
                retry_result = legacy and getattr(result, "part_kind", None) == "retry-prompt"
                content = getattr(result, "content", result) if legacy else result
                span.set_attribute(A.TOOL_RESULT, _serialize(content))
                span.set_attribute(A.TOOL_IS_ERROR, retry_result)
                if retry_result:
                    message = _serialize(content)
                    span.set_attribute(A.TOOL_ERROR_MESSAGE, message)
                    span.set_status(Status(StatusCode.ERROR, message))
                else:
                    span.set_status(Status(StatusCode.OK))
                    _capture(media, "tool_result", span, content, tool_call_id)
                return result
            finally:
                span.set_attribute(A.TOOL_DURATION_MS, (time.perf_counter() - start) * 1000)

    return wrapper
