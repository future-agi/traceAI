"""Trace the dedicated Speakeasy-generated ``openrouter`` Python SDK.

Instrument only inference endpoints. Do not instrument HTTPX or OpenAI alongside
this instrumentor for the same request; those paths would create duplicate spans.
"""
from __future__ import annotations

import json
from importlib import import_module
from typing import Any, Collection

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes as A
from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace import Status, StatusCode
from wrapt import wrap_function_wrapper

from .version import __version__

_ENDPOINTS = (
    ("openrouter.chat", "Chat", "chat", "send"),
    ("openrouter.embeddings", "Embeddings", "embeddings", "generate"),
    ("openrouter.responses", "Responses", "responses", "send"),
    ("openrouter.beta_responses", "BetaResponses", "responses", "send"),
)
_LIMIT = 32768


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _text(value: Any) -> str:
    """Capture bounded textual output only, never embedding vectors or images."""
    choices = _field(value, "choices")
    if choices:
        first = choices[0]
        msg = _field(first, "delta") or _field(first, "message")
        content = _field(msg, "content")
        return content[:_LIMIT] if isinstance(content, str) else ""
    output_text = _field(value, "output_text")
    if isinstance(output_text, str) and output_text:
        return output_text[:_LIMIT]
    output = _field(value, "output")
    if output:
        parts = []
        for item in output:
            for block in _field(item, "content") or []:
                text = _field(block, "text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)[:_LIMIT]
    return ""


def _usage(span: Any, value: Any) -> None:
    usage = _field(value, "usage")
    if not usage:
        return
    for attr, names in (
        (A.GEN_AI_USAGE_INPUT_TOKENS, ("prompt_tokens", "input_tokens")),
        (A.GEN_AI_USAGE_OUTPUT_TOKENS, ("completion_tokens", "output_tokens")),
    ):
        for name in names:
            number = _field(usage, name)
            if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
                span.set_attribute(attr, number)
                break


def _result(span: Any, value: Any, content: bool) -> None:
    model = _field(value, "model")
    if isinstance(model, str):
        span.set_attribute(A.GEN_AI_RESPONSE_MODEL, model)
    _usage(span, value)
    if content:
        text = _text(value)
        if text:
            span.set_attribute(A.OUTPUT_VALUE, text)


def _input(span: Any, operation: str, kwargs: dict[str, Any], content: bool) -> None:
    if not content:
        return
    value = kwargs.get("messages") if operation == "chat" else kwargs.get("input")
    if value is None or operation == "embeddings":
        return  # Embedding input can contain arbitrary binary/image payloads.
    try:
        serialized = json.dumps(value, default=lambda obj: obj.model_dump(exclude_none=True))
    except (TypeError, ValueError, AttributeError):
        return
    span.set_attribute(A.INPUT_VALUE, serialized[:_LIMIT])
    span.set_attribute(A.INPUT_MIME_TYPE, "application/json")


def _failure(span: Any, exc: BaseException) -> None:
    # Exception text and stack traces may contain the API key, request body or URL.
    span.set_attribute("error.type", type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR))
    span.end()


class _Stream:
    def __init__(self, source: Any, span: Any, content: bool):
        self._source, self._span, self._content = source, span, content
        self._ended = False
        self._pieces: list[str] = []
        self._length = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def _observe(self, chunk: Any) -> None:
        # Responses SSE events carry final usage on response.completed, not on
        # the event itself. The final response supersedes individual deltas.
        event_type = _field(chunk, "type")
        completed = _field(chunk, "response")
        if event_type in ("response.completed", "response.failed") and completed:
            _result(self._span, completed, False)
            if event_type == "response.failed":
                self._span.set_status(Status(StatusCode.ERROR))
            if self._content:
                text = _text(completed)
                if text:
                    self._pieces = [text[:_LIMIT]]
                    self._length = len(self._pieces[0])
            return
        _result(self._span, chunk, False)
        if self._content and self._length < _LIMIT:
            text = _field(chunk, "delta") if event_type == "response.output_text.delta" else _text(chunk)
            if isinstance(text, str):
                text = text[: _LIMIT - self._length]
                self._pieces.append(text)
                self._length += len(text)

    def _finish(self, complete: bool = True) -> None:
        if not self._ended:
            self._ended = True
            if not complete:
                self._span.set_attribute("openrouter.stream.incomplete", True)
            if self._pieces and complete:
                self._span.set_attribute(A.OUTPUT_VALUE, "".join(self._pieces))
            self._span.end()

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._source)
        except StopIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._ended = True
            _failure(self._span, exc)
            raise
        self._observe(chunk)
        return chunk

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self._source.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._ended = True
            _failure(self._span, exc)
            raise
        self._observe(chunk)
        return chunk

    def close(self):
        try:
            return self._source.close()
        finally:
            self._finish(complete=False)

    def __enter__(self):
        self._source.__enter__()
        return self

    def __exit__(self, *args):
        try:
            return self._source.__exit__(*args)
        finally:
            self._finish(complete=False)

    async def __aenter__(self):
        await self._source.__aenter__()
        return self

    async def __aexit__(self, *args):
        try:
            return await self._source.__aexit__(*args)
        finally:
            self._finish(complete=False)


class _AsyncStream(_Stream):
    async def close(self):
        try:
            return await self._source.close()
        finally:
            self._finish(complete=False)


def _start(tracer: Any, operation: str, kwargs: dict[str, Any], config: TraceConfig):
    kind = FiSpanKindValues.EMBEDDING if operation == "embeddings" else FiSpanKindValues.LLM
    model = kwargs.get("model")
    attrs = {A.GEN_AI_PROVIDER_NAME: "openrouter", A.GEN_AI_OPERATION_NAME: operation}
    if isinstance(model, str):
        attrs[A.GEN_AI_REQUEST_MODEL] = model
    span = tracer.start_span("openrouter." + operation, attributes=attrs, fi_span_kind=kind)
    _input(span, operation, kwargs, not (config.hide_inputs or config.hide_input_text))
    return span


def _wrap(tracer: Any, operation: str, config: TraceConfig, is_async: bool):
    def complete(span, result):
        if hasattr(result, "__anext__"):
            return _AsyncStream(result, span, not (config.hide_outputs or config.hide_output_text))
        if hasattr(result, "__next__"):
            return _Stream(result, span, not (config.hide_outputs or config.hide_output_text))
        _result(span, result, not (config.hide_outputs or config.hide_output_text))
        span.end()
        return result

    if is_async:
        async def wrapped(wrapped, instance, args, kwargs):
            span = _start(tracer, operation, kwargs, config)
            try:
                return complete(span, await wrapped(*args, **kwargs))
            except BaseException as exc:
                _failure(span, exc)
                raise
    else:
        def wrapped(wrapped, instance, args, kwargs):
            span = _start(tracer, operation, kwargs, config)
            try:
                return complete(span, wrapped(*args, **kwargs))
            except BaseException as exc:
                _failure(span, exc)
                raise
    return wrapped


class OpenRouterInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Instrument dedicated OpenRouter chat, embeddings and Responses calls."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("openrouter >= 1.3.0, < 2",)

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config") or TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        tracer = FITracer(trace.get_tracer(__name__, __version__, kwargs.get("tracer_provider")), config)
        self._originals = []
        for module, cls_name, operation, method in _ENDPOINTS:
            cls = getattr(import_module(module), cls_name)
            for name, is_async in ((method, False), (method + "_async", True)):
                original = cls.__dict__[name]
                wrap_function_wrapper(module, cls_name + "." + name, _wrap(tracer, operation, config, is_async))
                self._originals.append((cls, name, original))

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, name, original in getattr(self, "_originals", []):
            setattr(cls, name, original)
        self._originals = []


__all__ = ["OpenRouterInstrumentor", "__version__"]
