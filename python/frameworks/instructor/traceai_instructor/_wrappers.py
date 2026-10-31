"""Trace extraction calls, not factories or SDK client objects."""

import inspect
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Optional

from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from pydantic import BaseModel
from wrapt import FunctionWrapper

# Only suppress our nested entry points, never provider instrumentation.
_ACTIVE = ContextVar("traceai_instructor_request", default=False)
logger = logging.getLogger(__name__)


def _json_value(value: Any) -> Any:
    """No vars(), repr(), SDK clients, callbacks, or arbitrary object traversal."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items() if isinstance(key, str)}
    return None


def _content(span, key, mime_key, value):
    # Custom Pydantic serializers must not be able to break the application.
    try:
        encoded = json.dumps(_json_value(value), ensure_ascii=False)
    except Exception as error:
        # Fail open without logging payload-bearing exception text/tracebacks.
        logger.warning("Instructor telemetry serialization failed (%s)", type(error).__name__)
        return
    span.set_attribute(key, encoded)
    span.set_attribute(mime_key, "application/json")


class _Request:
    def __init__(self, tracer, name, kwargs, hooks_class):
        self.span = tracer.start_span(name, attributes={
            SpanAttributes.GEN_AI_SPAN_KIND: FiSpanKindValues.CHAIN.value,
        })
        self.hooks_class = hooks_class
        self.with_completion = name.endswith(".create_with_completion")
        self.attempts = 0
        self.validation_errors = 0
        self.usage = {}
        self.finished = False
        self.chunks = 0
        self.last = None
        if isinstance(kwargs.get("model"), str):
            self.span.set_attribute(SpanAttributes.GEN_AI_REQUEST_MODEL, kwargs["model"])
        # Allowlist request content. Never record kwargs, headers, context or clients.
        if "messages" in kwargs:
            _content(self.span, SpanAttributes.INPUT_VALUE, SpanAttributes.INPUT_MIME_TYPE,
                     kwargs["messages"])

    @contextmanager
    def activate(self):
        token = _ACTIVE.set(True)
        try:
            with trace.use_span(self.span, end_on_exit=False,
                                record_exception=False, set_status_on_exception=False):
                yield
        finally:
            _ACTIVE.reset(token)

    def arguments(self, args, kwargs, bound=None):
        if self.hooks_class is None:
            return args, kwargs
        hooks = self.hooks_class()
        existing = bound.arguments.get("hooks", kwargs.get("hooks")) if bound else kwargs.get("hooks")
        if existing is not None and not hasattr(type(existing), "__add__"):
            # Forward through the public emit API. Older Hooks has no __add__;
            # never mutate a shared handlers dictionary or close over a client.
            for name in ("completion:kwargs", "completion:response", "parse:error",
                         "completion:error", "completion:last_attempt", "completion:usage"):
                try:
                    event = hooks.get_hook_name(name)
                    def forward(*args, _event=event, **kwargs):
                        existing.emit(_event, *args, **kwargs)
                    hooks.on(name, forward)
                except ValueError:
                    pass
        hooks.on("completion:kwargs", self.on_attempt)
        hooks.on("completion:response", self.on_response)
        hooks.on("parse:error", self.on_parse_error)
        try:
            hooks.on("completion:usage", self.on_usage)
        except ValueError:
            pass  # Older hook APIs expose only per-completion usage.
        if existing is not None and hasattr(type(existing), "__add__"):
            hooks = existing + hooks
        if bound is not None and "hooks" in bound.signature.parameters:
            bound.arguments["hooks"] = hooks
            return bound.args, bound.kwargs
        return args, {**kwargs, "hooks": hooks}

    def on_attempt(self, *args, **kwargs):
        self.attempts += 1
        self.span.set_attribute("instructor.attempts", self.attempts)

    def on_parse_error(self, *args, **kwargs):
        self.validation_errors += 1
        # Counts only: validation exceptions may contain response content.
        self.span.set_attribute("instructor.validation_errors", self.validation_errors)

    def on_response(self, response, **kwargs):
        self._usage(getattr(response, "usage", None), cumulative=False)

    def on_usage(self, usage, **kwargs):
        self._usage(usage, cumulative=True)

    def _usage(self, usage, cumulative):
        for key, names in (
            ("gen_ai.usage.input_tokens", ("prompt_tokens", "input_tokens")),
            ("gen_ai.usage.output_tokens", ("completion_tokens", "output_tokens")),
            ("gen_ai.usage.total_tokens", ("total_tokens",)),
        ):
            for name in names:
                value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
                if isinstance(value, int):
                    self.usage[key] = value if cumulative else self.usage.get(key, 0) + value
                    self.span.set_attribute(key, self.usage[key])
                    break

    def result(self, value):
        if isinstance(value, AsyncIterator):
            return _AsyncStream(value, self)
        if isinstance(value, Iterator):
            return _Stream(value, self)
        # create_with_completion returns (parsed_model, raw_sdk_response).
        output = value[0] if self.with_completion and isinstance(value, tuple) and len(value) == 2 else value
        _content(self.span, SpanAttributes.OUTPUT_VALUE, SpanAttributes.OUTPUT_MIME_TYPE, output)
        self.finish()
        return value

    def chunk(self, value):
        self.chunks += 1
        # Bounded memory for arbitrarily long streams; don't accumulate chunks.
        self.last = value
        return value

    def finish(self, error=None, closed=False):
        if self.finished:
            return
        self.finished = True
        if self.chunks:
            self.span.set_attribute("instructor.stream.chunks", self.chunks)
            _content(self.span, SpanAttributes.OUTPUT_VALUE, SpanAttributes.OUTPUT_MIME_TYPE, self.last)
        if error is not None:
            self.span.set_status(trace.Status(trace.StatusCode.ERROR, str(error)))
            self.span.record_exception(error)
        elif closed:
            self.span.set_attribute("instructor.stream.closed_early", True)
        else:
            self.span.set_status(trace.StatusCode.OK)
        self.span.end()


class _Stream:
    def __init__(self, stream, request):
        self._stream, self._request = stream, request

    def __iter__(self):
        return self

    def __next__(self):
        return self._advance(next, self._stream)

    def send(self, value):
        return self._advance(self._stream.send, value)

    def throw(self, *args):
        return self._advance(self._stream.throw, *args)

    def _advance(self, function, *args):
        with self._request.activate():
            try:
                return self._request.chunk(function(*args))
            except StopIteration:
                self._request.finish()
                raise
            except BaseException as error:
                self._request.finish(error)
                raise

    def close(self):
        with self._request.activate():
            try:
                close = getattr(self._stream, "close", None)
                if close is not None:
                    close()
            except BaseException as error:
                self._request.finish(error)
                raise
            finally:
                self._request.finish(closed=True)


class _AsyncStream:
    def __init__(self, stream, request):
        self._stream, self._request = stream, request

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._advance(self._stream.__anext__)

    async def asend(self, value):
        return await self._advance(self._stream.asend, value)

    async def athrow(self, *args):
        return await self._advance(self._stream.athrow, *args)

    async def _advance(self, function, *args):
        with self._request.activate():
            try:
                return self._request.chunk(await function(*args))
            except StopAsyncIteration:
                self._request.finish()
                raise
            except BaseException as error:
                self._request.finish(error)
                raise

    async def aclose(self):
        with self._request.activate():
            try:
                close = getattr(self._stream, "aclose", None)
                if close is not None:
                    await close()
            except BaseException as error:
                self._request.finish(error)
                raise
            finally:
                self._request.finish(closed=True)


class _RequestWrapper:
    def __init__(self, tracer: trace.Tracer, hooks_class: Optional[type] = None,
                 name: str = "instructor.create") -> None:
        self.tracer = tracer
        self.hooks_class = hooks_class
        self.name = name
        self.enabled = True

    def __call__(self, wrapped: Callable[..., Any], instance: Any,
                 args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        if not self.enabled or _ACTIVE.get() or context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        bound = None
        values = kwargs
        if args:
            try:
                bound = inspect.signature(wrapped, follow_wrapped=False).bind(*args, **kwargs)
                values = {**kwargs, **bound.arguments}
            except (TypeError, ValueError):
                pass  # Let the application callable report invalid arguments.
        if inspect.iscoroutinefunction(wrapped):
            async def invoke():
                if not self.enabled or _ACTIVE.get() or context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
                    return await wrapped(*args, **kwargs)
                request = _Request(self.tracer, self.name, values, self.hooks_class)
                with request.activate():
                    try:
                        call_args, call_kwargs = request.arguments(args, kwargs, bound)
                        return request.result(await wrapped(*call_args, **call_kwargs))
                    except BaseException as error:
                        request.finish(error)
                        raise
            return invoke()
        request = _Request(self.tracer, self.name, values, self.hooks_class)
        with request.activate():
            try:
                call_args, call_kwargs = request.arguments(args, kwargs, bound)
                return request.result(wrapped(*call_args, **call_kwargs))
            except BaseException as error:
                request.finish(error)
                raise


class _FactoryWrapper:
    """Wrap generated callables without tracing/serializing the factory result."""

    def __init__(self, request_wrapper: _RequestWrapper, legacy: bool = False) -> None:
        self.request_wrapper = request_wrapper
        self.legacy = legacy

    def __call__(self, wrapped: Callable[..., Any], instance: Any,
                 args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        result = wrapped(*args, **kwargs)
        if not self.request_wrapper.enabled:
            return result
        if self.legacy and not callable(result):
            result.chat.completions.create = FunctionWrapper(
                result.chat.completions.create, self.request_wrapper)
            return result
        return FunctionWrapper(result, self.request_wrapper)


def _remove_wrapper(owner: Any, name: str, hook: FunctionWrapper) -> None:
    """Detach only our dispatch link; decorators closing over it remain inert."""
    current = inspect.getattr_static(owner, name)
    if current is hook:
        setattr(owner, name, hook.__wrapped__)
        return
    seen = set()
    while hasattr(current, "__wrapped__") and id(current) not in seen:
        seen.add(id(current))
        if current.__wrapped__ is hook:
            if isinstance(current, FunctionWrapper):
                current.__wrapped__ = hook.__wrapped__
            return
        current = current.__wrapped__
