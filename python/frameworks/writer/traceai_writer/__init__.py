"""Trace the Writer Python SDK (``writer-sdk``, import ``writerai``).

Chat (including tool calling and streaming) and legacy text completions are traced, sync and
async. Only the outermost SDK call produces a span: ``client.chat.stream()`` and the
``with_raw_response`` / ``with_streaming_response`` variants all end up in the one wrapped
method. Do not instrument HTTPX alongside this instrumentor for the same requests.
"""
from __future__ import annotations

import json
import logging
from importlib import import_module
from typing import Any, Collection, Dict, List, Optional

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes as A
from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, get_value
from opentelemetry.trace import Status, StatusCode
from wrapt import wrap_function_wrapper

from .version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_PROVIDER = "writer"
_LIMIT = 32768
_OUT = f"{A.GEN_AI_OUTPUT_MESSAGES}.0.message"

# (module, sync class, async class, method, operation)
_ENDPOINTS = (
    ("writerai.resources.chat", "ChatResource", "AsyncChatResource", "chat", "chat"),
    ("writerai.resources.completions", "CompletionsResource", "AsyncCompletionsResource", "create", "text_completion"),
)
_PARAMETERS = (
    ("temperature", A.GEN_AI_REQUEST_TEMPERATURE),
    ("top_p", A.GEN_AI_REQUEST_TOP_P),
    ("max_tokens", A.GEN_AI_REQUEST_MAX_TOKENS),
)


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _count(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


class _Recorder:
    """Folds a response, or the chunks of a streamed response, into span attributes."""

    def __init__(self, operation: str, capture_output: bool) -> None:
        self.operation = operation
        self.capture_output = capture_output
        self.model: Optional[str] = None
        self.response_id: Optional[str] = None
        self.finish: List[str] = []
        self.usage: Dict[str, int] = {}
        self.pieces: List[str] = []
        self.length = 0
        self.tools: Dict[int, Dict[str, Any]] = {}

    def _add(self, text: Optional[str]) -> None:
        if text and self.length < _LIMIT:
            text = text[: _LIMIT - self.length]
            self.pieces.append(text)
            self.length += len(text)

    def _tool_calls(self, calls: Any) -> None:
        if not isinstance(calls, (list, tuple)):
            return
        for position, call in enumerate(calls):
            index = _count(_field(call, "index"))
            slot = self.tools.setdefault(position if index is None else index, {"args": []})
            if _text(_field(call, "id")):
                slot["id"] = _field(call, "id")
            function = _field(call, "function")
            if _text(_field(function, "name")):
                slot["name"] = _field(function, "name")
            arguments = _field(function, "arguments")
            if isinstance(arguments, str):
                slot["args"].append(arguments)

    def observe(self, value: Any, chunk: bool = False) -> None:
        """Fold a ChatCompletion, Completion or a streamed chunk into the recorder."""
        if _text(_field(value, "model")):
            self.model = _field(value, "model")
        if _text(_field(value, "id")):
            self.response_id = _field(value, "id")
        usage = _field(value, "usage")
        if usage is not None:
            for key, names in (
                ("input", ("prompt_tokens", "input_tokens")),
                ("output", ("completion_tokens", "output_tokens")),
                ("total", ("total_tokens",)),
            ):
                for name in names:
                    number = _count(_field(usage, name))
                    if number is not None:
                        self.usage[key] = number
                        break
        choices = _field(value, "choices")
        if not isinstance(choices, (list, tuple)):
            self._add(_text(_field(value, "value")))  # legacy completion stream chunk
            return
        for position, choice in enumerate(choices):
            reason = _field(choice, "finish_reason")
            if _text(reason) and reason not in self.finish:
                self.finish.append(reason)
            if position:
                continue  # content comes from the first choice
            if self.operation == "text_completion":
                self._add(_text(_field(choice, "text")))
                continue
            holder = _field(choice, "delta") if chunk else _field(choice, "message")
            if holder is None:
                holder = _field(choice, "delta") or _field(choice, "message")
            if holder is not None:
                self._add(_text(_field(holder, "content")))
                self._tool_calls(_field(holder, "tool_calls"))

    def apply(self, span: Any) -> None:
        if self.model:
            span.set_attribute(A.GEN_AI_RESPONSE_MODEL, self.model)
        if self.response_id:
            span.set_attribute(A.GEN_AI_RESPONSE_ID, self.response_id)
        if self.finish:
            span.set_attribute(A.GEN_AI_RESPONSE_FINISH_REASONS, self.finish)
        total = self.usage.get("total")
        if total is None and "input" in self.usage and "output" in self.usage:
            total = self.usage["input"] + self.usage["output"]
        for attr, value in (
            (A.GEN_AI_USAGE_INPUT_TOKENS, self.usage.get("input")),
            (A.GEN_AI_USAGE_OUTPUT_TOKENS, self.usage.get("output")),
            (A.GEN_AI_USAGE_TOTAL_TOKENS, total),
        ):
            if value is not None:
                span.set_attribute(attr, value)
        for position, slot in sorted(self.tools.items()):
            prefix = f"{_OUT}.tool_calls.{position}.tool_call"
            if slot.get("id"):
                span.set_attribute(f"{prefix}.id", slot["id"])
            if slot.get("name"):
                span.set_attribute(f"{prefix}.function.name", slot["name"])
            arguments = "".join(slot["args"])
            if self.capture_output and arguments:
                span.set_attribute(f"{prefix}.function.arguments", arguments[:_LIMIT])
        if self.capture_output and self.pieces:
            span.set_attribute(A.OUTPUT_VALUE, "".join(self.pieces))


def _request(span: Any, operation: str, kwargs: Dict[str, Any], capture_input: bool) -> None:
    for name, attr in _PARAMETERS:
        value = kwargs.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            span.set_attribute(attr, value)
    stop = kwargs.get("stop")
    if isinstance(stop, str):
        stop = [stop]
    if isinstance(stop, (list, tuple)) and stop and all(isinstance(s, str) for s in stop):
        span.set_attribute(A.GEN_AI_REQUEST_STOP_SEQUENCES, list(stop))
    if not capture_input:
        return
    if operation == "text_completion":
        prompt = kwargs.get("prompt")
        if isinstance(prompt, str) and "data:image/" not in prompt.lower():
            span.set_attribute(A.INPUT_VALUE, prompt[:_LIMIT])
            span.set_attribute(A.INPUT_MIME_TYPE, "text/plain")
        return
    messages = kwargs.get("messages")
    # Only plain text conversations are serialized wholesale: input.value cannot tell text
    # from an inline image, document or tool payload inside a structured content part.
    if not isinstance(messages, (list, tuple)) or any(
        not isinstance(m, dict) or not isinstance(m.get("role"), str) or not isinstance(m.get("content"), str)
        for m in messages
    ):
        return
    plain = [{"role": m["role"], "content": m["content"]} for m in messages]
    serialized = json.dumps(plain)
    if "data:image/" in serialized.lower():
        return
    span.set_attribute(A.INPUT_VALUE, serialized[:_LIMIT])
    span.set_attribute(A.INPUT_MIME_TYPE, "application/json")


def _failure(span: Any, exc: BaseException) -> None:
    # Exception text can contain the request, response body or credentials: class name only.
    span.set_attribute("error.type", type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR))
    span.end()


class _Response:
    """The httpx response behind a stream, closed by the SDK's own helpers.

    ``chat.stream()`` keeps ``raw_stream.response`` and closes that, never the stream, so the
    span has to end when the response is closed too.
    """

    def __init__(self, response: Any, finish: Any) -> None:
        self._response, self._finish = response, finish

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    @property
    def __class__(self) -> Any:  # type: ignore[override]
        return type(self._response)  # isinstance(proxy, httpx.Response) keeps working

    def close(self) -> Any:
        try:
            return self._response.close()
        finally:
            self._finish(complete=False)

    async def aclose(self) -> Any:
        try:
            return await self._response.aclose()
        finally:
            self._finish(complete=False)


class _Stream:
    """Proxy for the SDK's Stream / AsyncStream; chunks are forwarded unchanged.

    The span ends on exhaustion, error, ``close()`` or leaving the ``with`` block. A stream that is
    simply dropped is never ended here: no finalizer ends or exports its span. A processor that
    tracks open spans may still end it at shutdown, marked ``gen_ai.span.leaked``.
    """

    def __init__(self, source: Any, span: Any, recorder: _Recorder) -> None:
        self._source, self._span, self._recorder = source, span, recorder
        self._ended = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    @property
    def response(self) -> Any:
        return _Response(self._source.response, self._finish)

    @property
    def __class__(self) -> Any:  # type: ignore[override]
        return type(self._source)  # isinstance(proxy, writerai.Stream) keeps working

    def _finish(self, complete: bool = True) -> None:
        if self._ended:
            return
        self._ended = True
        try:
            if not complete:
                self._span.set_attribute("writer.stream.incomplete", True)
            self._recorder.apply(self._span)
        except Exception:
            logger.debug("writer stream finalisation failed", exc_info=True)
        finally:
            self._span.end()

    def _fail(self, exc: BaseException) -> None:
        if not self._ended:
            self._ended = True
            try:
                self._span.set_attribute("writer.stream.incomplete", True)
                self._recorder.apply(self._span)  # what arrived before the failure
            except Exception:
                logger.debug("writer stream finalisation failed", exc_info=True)
            _failure(self._span, exc)

    def _observe(self, chunk: Any) -> None:
        try:
            self._recorder.observe(chunk, chunk=True)
        except Exception:
            logger.debug("writer stream capture failed", exc_info=True)

    def __iter__(self) -> "_Stream":
        return self

    def __next__(self) -> Any:
        try:
            with trace.use_span(self._span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                chunk = next(self._source)
        except StopIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._fail(exc)
            raise
        self._observe(chunk)
        return chunk

    def __aiter__(self) -> "_Stream":
        return self

    async def __anext__(self) -> Any:
        try:
            with trace.use_span(self._span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                chunk = await self._source.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as exc:
            self._fail(exc)
            raise
        self._observe(chunk)
        return chunk

    def close(self) -> Any:
        try:
            return self._source.close()
        finally:
            self._finish(complete=False)

    def __enter__(self) -> "_Stream":
        self._source.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        try:
            return self._source.__exit__(*exc_info)
        finally:
            self._finish(complete=False)

    async def __aenter__(self) -> "_Stream":
        await self._source.__aenter__()
        return self

    async def __aexit__(self, *exc_info: Any) -> Any:
        try:
            return await self._source.__aexit__(*exc_info)
        finally:
            self._finish(complete=False)


class _AsyncStream(_Stream):
    async def close(self) -> Any:  # type: ignore[override]
        try:
            return await self._source.close()
        finally:
            self._finish(complete=False)


def _start(tracer: Any, config: TraceConfig, operation: str, kwargs: Dict[str, Any]) -> Any:
    attrs: Dict[str, Any] = {A.GEN_AI_PROVIDER_NAME: _PROVIDER, A.GEN_AI_OPERATION_NAME: operation}
    model = kwargs.get("model")
    if isinstance(model, str) and model:
        attrs[A.GEN_AI_REQUEST_MODEL] = model
    span = tracer.start_span(f"writer.{operation}", attributes=attrs, fi_span_kind=FiSpanKindValues.LLM)
    try:
        capture_input = not (
            config.hide_inputs or config.hide_input_text or config.hide_input_messages or config.hide_input_images
        )
        _request(span, operation, kwargs, capture_input)
    except Exception:
        logger.debug("writer request capture failed", exc_info=True)
    return span


def _complete(span: Any, result: Any, operation: str, config: TraceConfig) -> Any:
    capture_output = not (config.hide_outputs or config.hide_output_text or config.hide_output_messages)
    recorder = _Recorder(operation, capture_output)
    try:
        if hasattr(result, "__anext__"):
            return _AsyncStream(result, span, recorder)
        if hasattr(result, "__next__"):
            return _Stream(result, span, recorder)
        if hasattr(result, "choices"):
            recorder.observe(result)
            recorder.apply(span)
        # Anything else is a raw or streaming-response wrapper: its body is not ours to read.
    except Exception:
        logger.debug("writer response capture failed", exc_info=True)
    span.end()
    return result


def _wrap(tracer: Any, config: TraceConfig, operation: str, is_async: bool) -> Any:
    if is_async:
        async def wrapped(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
            if get_value(_SUPPRESS_INSTRUMENTATION_KEY):
                return await wrapped(*args, **kwargs)
            span = _start(tracer, config, operation, kwargs)
            try:
                with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                    result = await wrapped(*args, **kwargs)
            except BaseException as exc:
                _failure(span, exc)
                raise
            return _complete(span, result, operation, config)
    else:
        def wrapped(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:  # type: ignore[misc]
            if get_value(_SUPPRESS_INSTRUMENTATION_KEY):
                return wrapped(*args, **kwargs)
            span = _start(tracer, config, operation, kwargs)
            try:
                with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                    result = wrapped(*args, **kwargs)
            except BaseException as exc:
                _failure(span, exc)
                raise
            return _complete(span, result, operation, config)
    return wrapped


class WriterInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Instrument Writer SDK chat and completions calls (sync and async)."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("writer-sdk >= 2.0.0, < 4",)

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config") or TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        tracer = FITracer(trace.get_tracer(__name__, __version__, kwargs.get("tracer_provider")), config)
        self._originals: List[Any] = []
        for module, sync_cls, async_cls, method, operation in _ENDPOINTS:
            mod = import_module(module)
            for cls_name, is_async in ((sync_cls, False), (async_cls, True)):
                cls = getattr(mod, cls_name)
                original = cls.__dict__[method]
                wrap_function_wrapper(module, f"{cls_name}.{method}", _wrap(tracer, config, operation, is_async))
                self._originals.append((cls, method, original))

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, name, original in getattr(self, "_originals", []):
            setattr(cls, name, original)
        self._originals = []


__all__ = ["WriterInstrumentor", "__version__"]
