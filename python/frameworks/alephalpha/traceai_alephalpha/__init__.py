"""Trace the Aleph Alpha Python SDK (``aleph-alpha-client``, import ``aleph_alpha_client``).

``Client`` and ``AsyncClient`` ``complete``, ``chat``, the embedding calls (``embed``, ``embeddings``,
``semantic_embed``, ``batch_semantic_embed``, ``instructable_embed``) and ``evaluate`` are traced, plus
the async streaming calls ``complete_with_streaming`` and ``chat_with_streaming`` (the sync client has
no streaming). Only the outermost SDK call produces a span. Methods an installed SDK version does not
have are skipped.
"""
from __future__ import annotations

import contextvars
import json
import logging
from enum import Enum
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

_PROVIDER = "aleph_alpha"
_LIMIT = 32768
_OUT = f"{A.GEN_AI_OUTPUT_MESSAGES}.0.message"
_MODULE = "aleph_alpha_client.aleph_alpha_client"

# method -> (operation, span kind)
_METHODS = {
    "complete": ("text_completion", FiSpanKindValues.LLM),
    "chat": ("chat", FiSpanKindValues.LLM),
    "evaluate": ("evaluate", FiSpanKindValues.LLM),
    "embed": ("embeddings", FiSpanKindValues.EMBEDDING),
    "embeddings": ("embeddings", FiSpanKindValues.EMBEDDING),
    "semantic_embed": ("embeddings", FiSpanKindValues.EMBEDDING),
    "batch_semantic_embed": ("embeddings", FiSpanKindValues.EMBEDDING),
    "instructable_embed": ("embeddings", FiSpanKindValues.EMBEDDING),
}
_STREAMS = {"complete_with_streaming": "text_completion", "chat_with_streaming": "chat"}

# Set while an SDK call we traced is running, so calls it makes into other traced methods are not spans.
_INSIDE: "contextvars.ContextVar[bool]" = contextvars.ContextVar("traceai_alephalpha_inside", default=False)


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


def _enum(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


class _Recorder:
    """Folds a response, or the items of a streamed response, into span attributes."""

    def __init__(self, operation: str, capture_output: bool) -> None:
        self.operation = operation
        self.capture_output = capture_output
        self.model: Optional[str] = None
        self.response_id: Optional[str] = None
        self.finish: List[str] = []
        self.usage: Dict[str, int] = {}
        self.pieces: List[str] = []
        self.length = 0
        self.tools: List[Dict[str, Any]] = []
        self.result: Any = None

    def _add(self, text: Optional[str]) -> None:
        if text and self.length < _LIMIT:
            text = text[: _LIMIT - self.length]
            self.pieces.append(text)
            self.length += len(text)

    def _finish(self, reason: Any) -> None:
        reason = _enum(reason)
        if _text(reason) and reason not in self.finish:
            self.finish.append(reason)

    def _tool_calls(self, calls: Any) -> None:
        if not isinstance(calls, (list, tuple)):
            return
        for call in calls:
            function = _field(call, "function")
            self.tools.append({
                "id": _text(_field(call, "id")),
                "name": _text(_field(function, "name")),
                "args": _field(function, "arguments") if isinstance(_field(function, "arguments"), str) else None,
            })

    def _usage(self, value: Any) -> None:
        for key, names in (
            ("input", ("prompt_tokens", "num_tokens_prompt_total")),
            ("output", ("completion_tokens", "num_tokens_generated")),
            ("total", ("total_tokens",)),
        ):
            for name in names:
                number = _count(_field(value, name))
                if number is not None:
                    self.usage[key] = number
                    break

    def observe(self, value: Any) -> None:
        """Fold a response object or a streamed item into the recorder."""
        if isinstance(value, str):  # a streamed FinishReason
            self._finish(value)
            return
        for name in ("model_version", "model"):
            if _text(_field(value, name)):
                self.model = _field(value, name)
                break
        if _text(_field(value, "id")):
            self.response_id = _field(value, "id")
        self._usage(value)
        usage = _field(value, "usage")
        if usage is not None:
            self._usage(usage)
        if self.operation == "embeddings":
            return
        if self.operation == "evaluate":
            result = _field(value, "result")
            if isinstance(result, dict):
                self.result = result
            return
        # complete: CompletionResponse, StreamChunk, StreamSummary
        completions = _field(value, "completions")
        if isinstance(completions, (list, tuple)):
            first = completions[0] if completions else None
            if first is not None:
                self._add(_text(_field(first, "completion")))
                self._finish(_field(first, "finish_reason"))
            return
        if _field(value, "completion") is not None and _field(value, "index") in (0, None):
            self._add(_text(_field(value, "completion")))
        # chat: ChatResponse, ChatStreamChunk, a streamed ToolCall, or an OpenAI-shaped body
        message = _field(value, "message")
        if message is not None:
            self._add(_text(_field(message, "content")))
            self._tool_calls(_field(message, "tool_calls"))
        elif _field(value, "function") is not None:
            self._tool_calls([value])
        elif _field(value, "content") is not None or _field(value, "tool_calls") is not None:
            self._add(_text(_field(value, "content")))
            self._tool_calls(_field(value, "tool_calls"))
        self._finish(_field(value, "finish_reason"))

    def apply(self, span: Any) -> None:
        if self.model:
            span.set_attribute(A.GEN_AI_RESPONSE_MODEL, self.model)
        if self.response_id:
            span.set_attribute(A.GEN_AI_RESPONSE_ID, self.response_id)
        if self.finish:
            span.set_attribute(A.GEN_AI_RESPONSE_FINISH_REASONS, self.finish)
        total = self.usage.get("total")
        if total is None and "input" in self.usage:
            total = self.usage["input"] + self.usage.get("output", 0)
        for attr, value in (
            (A.GEN_AI_USAGE_INPUT_TOKENS, self.usage.get("input")),
            (A.GEN_AI_USAGE_OUTPUT_TOKENS, self.usage.get("output")),
            (A.GEN_AI_USAGE_TOTAL_TOKENS, total),
        ):
            if value is not None:
                span.set_attribute(attr, value)
        for position, slot in enumerate(self.tools):
            prefix = f"{_OUT}.tool_calls.{position}.tool_call"
            if slot["id"]:
                span.set_attribute(f"{prefix}.id", slot["id"])
            if slot["name"]:
                span.set_attribute(f"{prefix}.function.name", slot["name"])
            if self.capture_output and slot["args"]:
                span.set_attribute(f"{prefix}.function.arguments", slot["args"][:_LIMIT])
        if self.capture_output:
            if self.pieces:
                span.set_attribute(A.OUTPUT_VALUE, "".join(self.pieces))
            elif self.result is not None:
                try:
                    span.set_attribute(A.OUTPUT_VALUE, json.dumps(self.result, default=str)[:_LIMIT])
                    span.set_attribute(A.OUTPUT_MIME_TYPE, "application/json")
                except Exception:
                    pass


def _prompt_text(prompt: Any) -> Optional[str]:
    """The text of a Prompt made only of Text items; images and token ids are never serialized."""
    items = _field(prompt, "items")
    if not isinstance(items, (list, tuple)) or not items:
        return None
    pieces = []
    for item in items:
        text = _field(item, "text")
        if not isinstance(text, str):
            return None
        pieces.append(text)
    return "".join(pieces)


def _argument(args: Any, kwargs: Dict[str, Any], position: int, name: str) -> Any:
    if name in kwargs:
        return kwargs[name]
    return args[position] if len(args) > position else None


def _input_value(operation: str, request: Any) -> Optional[Any]:
    if operation == "text_completion":
        return _prompt_text(_field(request, "prompt"))
    if operation == "chat":
        messages = _field(request, "messages")
        if not isinstance(messages, (list, tuple)):
            return None
        plain = []
        for message in messages:
            role, content = _enum(_field(message, "role")), _field(message, "content")
            if not isinstance(role, str) or not isinstance(content, str):
                return None  # structured or multimodal content cannot be told from text
            plain.append({"role": role, "content": content})
        return plain
    if operation == "evaluate":
        prompt, expected = _prompt_text(_field(request, "prompt")), _field(request, "completion_expected")
        if prompt is None or not isinstance(expected, str):
            return None
        return {"prompt": prompt, "completion_expected": expected}
    # embeddings
    prompts = _field(request, "prompts")
    if isinstance(prompts, (list, tuple)):
        texts = [_prompt_text(p) for p in prompts]
        return texts if texts and all(t is not None for t in texts) else None
    for name in ("prompt", "input"):
        value = _field(request, name)
        if value is None:
            continue
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return value if value and all(isinstance(v, str) for v in value) else None
        return _prompt_text(value)
    return None


def _request(span: Any, operation: str, request: Any, capture_input: bool) -> None:
    for name, attr in (
        ("maximum_tokens", A.GEN_AI_REQUEST_MAX_TOKENS),
        ("temperature", A.GEN_AI_REQUEST_TEMPERATURE),
        ("top_p", A.GEN_AI_REQUEST_TOP_P),
        ("top_k", A.GEN_AI_REQUEST_TOP_K),
        ("presence_penalty", A.GEN_AI_REQUEST_PRESENCE_PENALTY),
        ("frequency_penalty", A.GEN_AI_REQUEST_FREQUENCY_PENALTY),
    ):
        value = _field(request, name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            span.set_attribute(attr, value)
    stop = _field(request, "stop_sequences")
    if isinstance(stop, (list, tuple)) and stop and all(isinstance(s, str) for s in stop):
        span.set_attribute(A.GEN_AI_REQUEST_STOP_SEQUENCES, list(stop))
    if not capture_input:
        return
    value = _input_value(operation, request)
    if value is None:
        return
    if isinstance(value, str):
        span.set_attribute(A.INPUT_VALUE, value[:_LIMIT])
        span.set_attribute(A.INPUT_MIME_TYPE, "text/plain")
    else:
        span.set_attribute(A.INPUT_VALUE, json.dumps(value)[:_LIMIT])
        span.set_attribute(A.INPUT_MIME_TYPE, "application/json")


def _failure(span: Any, exc: BaseException) -> None:
    # Exception text can contain the request, response body or credentials: class name only.
    span.set_attribute("error.type", type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR))
    span.end()


class _Stream:
    """Proxy for the SDK's async-generator streams; items are forwarded unchanged."""

    def __init__(self, source: Any, span: Any, recorder: _Recorder) -> None:
        self._source, self._span, self._recorder = source, span, recorder
        self._ended = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def __del__(self) -> None:
        # A stream dropped without being read to the end or closed must not leak an open span.
        try:
            self._finish(complete=False)
        except Exception:
            pass

    def _finish(self, complete: bool = True) -> None:
        if self._ended:
            return
        self._ended = True
        try:
            if not complete:
                self._span.set_attribute("alephalpha.stream.incomplete", True)
            self._recorder.apply(self._span)
        except Exception:
            logger.debug("aleph alpha stream finalisation failed", exc_info=True)
        finally:
            self._span.end()

    def __aiter__(self) -> "_Stream":
        return self

    async def __anext__(self) -> Any:
        try:
            with trace.use_span(self._span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                item = await self._source.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as exc:
            if not self._ended:
                self._ended = True
                _failure(self._span, exc)
            raise
        try:
            self._recorder.observe(item)
        except Exception:
            logger.debug("aleph alpha stream capture failed", exc_info=True)
        return item

    async def aclose(self) -> Any:
        try:
            return await self._source.aclose()
        finally:
            self._finish(complete=False)

    async def athrow(self, *args: Any) -> Any:
        try:
            return await self._source.athrow(*args)
        except BaseException as exc:
            if not self._ended:
                self._ended = True
                _failure(self._span, exc)
            raise

    async def asend(self, value: Any) -> Any:
        return await self.__anext__()


def _start(tracer: Any, config: TraceConfig, operation: str, kind: Any, args: Any, kwargs: Dict[str, Any]) -> Any:
    attrs: Dict[str, Any] = {A.GEN_AI_PROVIDER_NAME: _PROVIDER, A.GEN_AI_OPERATION_NAME: operation}
    request = _argument(args, kwargs, 0, "request")
    model = _argument(args, kwargs, 1, "model")
    if not isinstance(model, str) or not model:
        model = _field(request, "model")
    if isinstance(model, str) and model:
        attrs[A.GEN_AI_REQUEST_MODEL] = model
        if kind == FiSpanKindValues.EMBEDDING:
            attrs[A.EMBEDDING_MODEL_NAME] = model
    span = tracer.start_span(f"alephalpha.{operation}", attributes=attrs, fi_span_kind=kind)
    try:
        capture_input = not (
            config.hide_inputs or config.hide_input_text or config.hide_input_messages or config.hide_input_images
        )
        _request(span, operation, request, capture_input)
    except Exception:
        logger.debug("aleph alpha request capture failed", exc_info=True)
    return span


def _complete(span: Any, result: Any, operation: str, config: TraceConfig) -> Any:
    try:
        recorder = _Recorder(operation, not (config.hide_outputs or config.hide_output_text or config.hide_output_messages))
        if hasattr(result, "__anext__"):
            return _Stream(result, span, recorder)
        recorder.observe(result)
        recorder.apply(span)
    except Exception:
        logger.debug("aleph alpha response capture failed", exc_info=True)
    span.end()
    return result


def _begin(tracer: Any, config: TraceConfig, operation: str, kind: Any, args: Any, kwargs: Dict[str, Any]) -> Any:
    try:
        return _start(tracer, config, operation, kind, args, kwargs)
    except Exception:
        logger.debug("aleph alpha span start failed", exc_info=True)
        return None


def _wrap(tracer: Any, config: TraceConfig, operation: str, kind: Any, is_async: bool) -> Any:
    def skip() -> bool:
        return bool(get_value(_SUPPRESS_INSTRUMENTATION_KEY)) or _INSIDE.get()

    if is_async:
        async def wrapped(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
            if skip():
                return await wrapped(*args, **kwargs)
            span = _begin(tracer, config, operation, kind, args, kwargs)
            if span is None:
                return await wrapped(*args, **kwargs)
            marker = _INSIDE.set(True)
            try:
                with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                    result = await wrapped(*args, **kwargs)
            except BaseException as exc:
                _failure(span, exc)
                raise
            finally:
                _INSIDE.reset(marker)
            return _complete(span, result, operation, config)
    else:
        def wrapped(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:  # type: ignore[misc]
            if skip():
                return wrapped(*args, **kwargs)
            span = _begin(tracer, config, operation, kind, args, kwargs)
            if span is None:
                return wrapped(*args, **kwargs)
            marker = _INSIDE.set(True)
            try:
                with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                    result = wrapped(*args, **kwargs)
            except BaseException as exc:
                _failure(span, exc)
                raise
            finally:
                _INSIDE.reset(marker)
            return _complete(span, result, operation, config)
    return wrapped


def _wrap_stream(tracer: Any, config: TraceConfig, operation: str) -> Any:
    def wrapped(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        stream = wrapped(*args, **kwargs)
        if get_value(_SUPPRESS_INSTRUMENTATION_KEY) or _INSIDE.get():
            return stream
        span = _begin(tracer, config, operation, FiSpanKindValues.LLM, args, kwargs)
        if span is None:
            return stream
        return _complete(span, stream, operation, config)

    return wrapped


class AlephAlphaInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """Instrument Aleph Alpha SDK complete, chat, embedding, evaluate and streaming calls."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("aleph-alpha-client >= 7.6.0, < 12",)

    def _instrument(self, **kwargs: Any) -> None:
        config = kwargs.get("config") or TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        tracer = FITracer(trace.get_tracer(__name__, __version__, kwargs.get("tracer_provider")), config)
        self._originals: List[Any] = []
        module = import_module(_MODULE)
        for cls_name, is_async in (("Client", False), ("AsyncClient", True)):
            cls = getattr(module, cls_name)
            for method, (operation, kind) in _METHODS.items():
                self._patch(cls, cls_name, method, _wrap(tracer, config, operation, kind, is_async))
            if is_async:
                for method, operation in _STREAMS.items():
                    self._patch(cls, cls_name, method, _wrap_stream(tracer, config, operation))

    def _patch(self, cls: Any, cls_name: str, method: str, wrapper: Any) -> None:
        original = cls.__dict__.get(method)
        if original is None:
            return  # this SDK version does not have the method
        wrap_function_wrapper(_MODULE, f"{cls_name}.{method}", wrapper)
        self._originals.append((cls, method, original))

    def _uninstrument(self, **kwargs: Any) -> None:
        for cls, name, original in getattr(self, "_originals", []):
            setattr(cls, name, original)
        self._originals = []


__all__ = ["AlephAlphaInstrumentor", "__version__"]
