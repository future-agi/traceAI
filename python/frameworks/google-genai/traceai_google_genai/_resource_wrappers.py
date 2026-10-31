"""Wrap public resource methods, not SDK transports or higher-level agents."""

import inspect
import logging
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from importlib import import_module
from typing import Any

from fi_instrumentation import TraceConfig, get_attributes_from_context
from opentelemetry import context, trace
from wrapt import ObjectProxy, wrap_function_wrapper

from ._resource_attributes import ResponseAttributes, request_attributes

logger = logging.getLogger(__name__)
_active = ContextVar("traceai_google_genai_resource_active", default=False)


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as error:  # noqa: BLE001 -- telemetry must not break SDK calls
        # Exception text and traceback source lines may contain application data.
        # Keep the failure visible without bypassing TraceConfig through logs.
        logger.warning(
            "Google GenAI telemetry extraction failed (%s)", type(error).__name__
        )
        return None


class _Call:
    def __init__(self, tracer, config, resource, method, kwargs):
        self.response = ResponseAttributes(resource, config)
        attrs = _safe(request_attributes, resource, method, kwargs, config) or {}
        attrs.update(_safe(lambda: dict(get_attributes_from_context())) or {})
        self.span = _safe(tracer.start_span, f"{resource}.{method}", attributes=attrs)
        if self.span is None:
            self.span = trace.INVALID_SPAN
        self.finished = False

    @contextmanager
    def current(self):
        token = _active.set(True)
        try:
            with trace.use_span(
                self.span,
                end_on_exit=False,
                record_exception=False,
                set_status_on_exception=False,
            ):
                yield
        finally:
            _active.reset(token)

    def finish(self, error=None, *, complete=False):
        if self.finished:
            return
        self.finished = True
        _safe(self.span.set_attributes, _safe(self.response.finish) or {})
        if error is not None or self.response.failed:
            # FITracer controls exception privacy; use a content-free status.
            if isinstance(error, Exception):
                _safe(self.span.record_exception, error)
            _safe(self.span.set_status, trace.Status(trace.StatusCode.ERROR))
        elif complete:
            _safe(self.span.set_status, trace.Status(trace.StatusCode.OK))
        _safe(self.span.end)


class _Stream(ObjectProxy):
    def __init__(self, stream, call):
        super().__init__(stream)
        self._self_call = call
        self._self_iterator = None

    def __iter__(self):
        return self

    def __next__(self):
        call = self._self_call
        try:
            with call.current():
                if self._self_iterator is None:
                    self._self_iterator = iter(self.__wrapped__)
                event = next(self._self_iterator)
        except StopIteration:
            call.finish(complete=True)
            raise
        except BaseException as error:
            call.finish(error)
            raise
        _safe(call.response.event, event)
        return event

    def close(self):
        call = self._self_call
        try:
            with call.current():
                return self.__wrapped__.close()
        except BaseException as error:
            call.finish(error)
            raise
        finally:
            call.finish()

    def __enter__(self):
        try:
            with self._self_call.current():
                self.__wrapped__.__enter__()
        except BaseException as error:
            self._self_call.finish(error)
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            with self._self_call.current():
                return self.__wrapped__.__exit__(exc_type, exc, tb)
        except BaseException as error:
            self._self_call.finish(error)
            raise
        finally:
            self._self_call.finish(exc)


class _AsyncStream(ObjectProxy):
    def __init__(self, stream, call):
        super().__init__(stream)
        self._self_call = call
        self._self_iterator = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        call = self._self_call
        try:
            with call.current():
                if self._self_iterator is None:
                    self._self_iterator = self.__wrapped__.__aiter__()
                event = await self._self_iterator.__anext__()
        except StopAsyncIteration:
            call.finish(complete=True)
            raise
        except BaseException as error:
            call.finish(error)
            raise
        _safe(call.response.event, event)
        return event

    async def close(self):
        call = self._self_call
        try:
            with call.current():
                close = (
                    getattr(self.__wrapped__, "aclose", None) or self.__wrapped__.close
                )
                result = close()
                if inspect.isawaitable(result):
                    return await result
                return result
        except BaseException as error:
            call.finish(error)
            raise
        finally:
            call.finish()

    async def aclose(self):
        return await self.close()

    async def __aenter__(self):
        try:
            with self._self_call.current():
                await self.__wrapped__.__aenter__()
        except BaseException as error:
            self._self_call.finish(error)
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            with self._self_call.current():
                return await self.__wrapped__.__aexit__(exc_type, exc, tb)
        except BaseException as error:
            self._self_call.finish(error)
            raise
        finally:
            self._self_call.finish(exc)


def _wrapper(tracer, config, resource, method, asynchronous):
    def suppressed():
        return _active.get() or context.get_value(context._SUPPRESS_INSTRUMENTATION_KEY)

    def finish(call, response):
        # Inspect the returned protocol, not only stream=True: new SDKs also
        # accept stream inside typed requests. Never iterate Pydantic responses.
        if resource == "interactions":
            if hasattr(response, "__anext__"):
                return _AsyncStream(response, call)
            if hasattr(response, "__next__"):
                return _Stream(response, call)
        _safe(call.response.response, response)
        call.finish(complete=True)
        return response

    def sync(wrapped, instance, args, kwargs):
        if suppressed():
            return wrapped(*args, **kwargs)
        call = _Call(tracer, config, resource, method, kwargs)
        try:
            with call.current():
                response = wrapped(*args, **kwargs)
        except BaseException as error:
            call.finish(error)
            raise
        return finish(call, response)

    async def async_(wrapped, instance, args, kwargs):
        if suppressed():
            return await wrapped(*args, **kwargs)
        call = _Call(tracer, config, resource, method, kwargs)
        try:
            with call.current():
                response = await wrapped(*args, **kwargs)
        except BaseException as error:
            call.finish(error)
            raise
        return finish(call, response)

    return async_ if asynchronous else sync


def instrument_resources(
    tracer: trace.Tracer, config: TraceConfig
) -> list[tuple[type, str, Callable[..., Any]]]:
    patches = []
    targets = [
        ("google.genai.models", "Models", "models", ("embed_content",), False),
        ("google.genai.models", "AsyncModels", "models", ("embed_content",), True),
        (
            "google.genai.caches",
            "Caches",
            "caches",
            ("create", "get", "update", "delete"),
            False,
        ),
        (
            "google.genai.caches",
            "AsyncCaches",
            "caches",
            ("create", "get", "update", "delete"),
            True,
        ),
        (
            "google.genai._interactions.resources.interactions",
            "InteractionsResource",
            "interactions",
            ("create", "get"),
            False,
        ),
        (
            "google.genai._interactions.resources.interactions",
            "AsyncInteractionsResource",
            "interactions",
            ("create", "get"),
            True,
        ),
        (
            "google.genai._gaos.google_genai",
            "GeminiNextGenInteractions",
            "interactions",
            ("create", "get"),
            False,
        ),
        (
            "google.genai._gaos.google_genai",
            "AsyncGeminiNextGenInteractions",
            "interactions",
            ("create", "get"),
            True,
        ),
    ]
    for module, class_name, resource, methods, asynchronous in targets:
        try:
            cls = getattr(import_module(module), class_name)
        except (ImportError, AttributeError):
            # Older SDKs do not have Interactions. Generation remains available.
            continue
        for method in methods:
            original = getattr(cls, method, None)
            if original is None:
                continue
            wrap_function_wrapper(
                module,
                f"{class_name}.{method}",
                _wrapper(tracer, config, resource, method, asynchronous),
            )
            patches.append((cls, method, original))
    return patches
