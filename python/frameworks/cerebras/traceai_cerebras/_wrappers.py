"""Native Cerebras chat/text tracing without changing the SDK response protocol."""

import logging
from collections.abc import Callable, Iterator, Mapping
from collections.abc import Iterator as IteratorABC
from contextlib import contextmanager
from typing import Any

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    FiSpanKindValues,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
    ToolCallAttributes,
)
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import INVALID_SPAN, Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue
from wrapt import ObjectProxy

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

CEREBRAS_QUEUE_TIME = "cerebras.queue_time"
CEREBRAS_PROMPT_TIME = "cerebras.prompt_time"
CEREBRAS_COMPLETION_TIME = "cerebras.completion_time"
CEREBRAS_TOTAL_TIME = "cerebras.total_time"

# Only request-body options: never serialize transport headers or credentials.
_PARAMETERS = (
    "max_tokens",
    "min_tokens",
    "temperature",
    "top_p",
    "stop",
    "seed",
    "n",
    "frequency_penalty",
    "presence_penalty",
    "response_format",
    "reasoning_effort",
    "reasoning_format",
    "tool_choice",
    "parallel_tool_calls",
    "stream_options",
    "best_of",
    "echo",
    "suffix",
    "logprobs",
    "top_logprobs",
)


def _get(value: Any, key: str, default: Any = None) -> Any:
    return (
        value.get(key, default)
        if isinstance(value, Mapping)
        else getattr(value, key, default)
    )


def _message_attributes(message: Any, prefix: str) -> dict[str, AttributeValue]:
    attributes = {}
    for field, key in (
        ("role", MessageAttributes.MESSAGE_ROLE),
        ("content", MessageAttributes.MESSAGE_CONTENT),
        ("name", MessageAttributes.MESSAGE_NAME),
        ("tool_call_id", MessageAttributes.MESSAGE_TOOL_CALL_ID),
    ):
        value = _get(message, field)
        if value is not None:
            attributes[f"{prefix}.{key}"] = (
                value if isinstance(value, str) else safe_json_dumps(value)
            )
    # Put reasoning under the message-content tree so the shared text/output
    # privacy switches apply to it just as they do to the final answer.
    if (reasoning := _get(message, "reasoning")) is not None:
        content_prefix = f"{prefix}.{MessageAttributes.MESSAGE_CONTENTS}.0"
        attributes[
            f"{content_prefix}.{MessageContentAttributes.MESSAGE_CONTENT_TYPE}"
        ] = "reasoning"
        attributes[
            f"{content_prefix}.{MessageContentAttributes.MESSAGE_CONTENT_TEXT}"
        ] = reasoning
    for index, call in enumerate(_get(message, "tool_calls") or []):
        call_prefix = f"{prefix}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}"
        if (call_id := _get(call, "id")) is not None:
            attributes[f"{call_prefix}.{ToolCallAttributes.TOOL_CALL_ID}"] = call_id
        function = _get(call, "function")
        for field, key in (
            ("name", ToolCallAttributes.TOOL_CALL_FUNCTION_NAME),
            ("arguments", ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON),
        ):
            if (value := _get(function, field)) is not None:
                attributes[f"{call_prefix}.{key}"] = (
                    value if isinstance(value, str) else safe_json_dumps(value)
                )
    return attributes


def _get_input_attributes(
    model: str, messages: Any = None, **kwargs: Any
) -> dict[str, AttributeValue]:
    attributes = {
        SpanAttributes.GEN_AI_SPAN_KIND: FiSpanKindValues.LLM.value,
        SpanAttributes.GEN_AI_PROVIDER_NAME: "cerebras",
        SpanAttributes.GEN_AI_REQUEST_MODEL: model,
        SpanAttributes.GEN_AI_OPERATION_NAME: "text_completion"
        if "prompt" in kwargs
        else "chat",
    }
    invocation = {
        key: kwargs[key] for key in _PARAMETERS if kwargs.get(key) is not None
    }
    if invocation:
        attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS] = safe_json_dumps(
            invocation
        )
    raw_input = {"model": model, **invocation}
    if "prompt" in kwargs:
        raw_input["prompt"] = kwargs["prompt"]
        attributes[SpanAttributes.GEN_AI_PROMPTS] = safe_json_dumps(kwargs["prompt"])
    else:
        raw_input["messages"] = messages or []
        for index, message in enumerate(messages or []):
            attributes.update(
                _message_attributes(
                    message, f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{index}"
                )
            )
    if (tools := kwargs.get("tools")) is not None:
        raw_input["tools"] = tools
        for index, tool in enumerate(tools):
            attributes[
                f"{SpanAttributes.GEN_AI_TOOL_DEFINITIONS}.{index}.tool.json_schema"
            ] = safe_json_dumps(tool)
    attributes[SpanAttributes.INPUT_VALUE] = safe_json_dumps(raw_input)
    attributes[SpanAttributes.INPUT_MIME_TYPE] = "application/json"
    return attributes


def _metadata_attributes(response: Any) -> dict[str, AttributeValue]:
    attributes = {}
    for field, key in (
        ("model", SpanAttributes.GEN_AI_RESPONSE_MODEL),
        ("id", SpanAttributes.GEN_AI_RESPONSE_ID),
    ):
        if (value := _get(response, field)) is not None:
            attributes[key] = value
    usage = _get(response, "usage")
    for field, key in (
        ("prompt_tokens", SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS),
        ("completion_tokens", SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS),
        ("total_tokens", SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS),
    ):
        if isinstance(value := _get(usage, field), (int, float)):
            attributes[key] = value
    for detail, field, key in (
        (
            "prompt_tokens_details",
            "cached_tokens",
            SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS_CACHE_READ,
        ),
        (
            "completion_tokens_details",
            "reasoning_tokens",
            SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS_REASONING,
        ),
    ):
        if isinstance(value := _get(_get(usage, detail), field), (int, float)):
            attributes[key] = value
    for field, key in (
        ("queue_time", CEREBRAS_QUEUE_TIME),
        ("prompt_time", CEREBRAS_PROMPT_TIME),
        ("completion_time", CEREBRAS_COMPLETION_TIME),
        ("total_time", CEREBRAS_TOTAL_TIME),
    ):
        if isinstance(value := _get(_get(response, "time_info"), field), (int, float)):
            attributes[key] = value
    return attributes


def _get_output_attributes(response: Any) -> dict[str, AttributeValue]:
    attributes = _metadata_attributes(response)
    finishes = []
    for position, choice in enumerate(_get(response, "choices") or []):
        index = _get(choice, "index", position)
        message = _get(choice, "message") or {
            "role": "assistant",
            "content": _get(choice, "text"),
        }
        attributes.update(
            _message_attributes(
                message, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}"
            )
        )
        if finish := _get(choice, "finish_reason"):
            finishes.append(finish)
    if finishes:
        attributes[SpanAttributes.GEN_AI_RESPONSE_FINISH_REASONS] = finishes
    if hasattr(response, "model_dump"):
        attributes[SpanAttributes.OUTPUT_VALUE] = safe_json_dumps(response.model_dump())
    elif isinstance(response, Mapping):
        attributes[SpanAttributes.OUTPUT_VALUE] = safe_json_dumps(response)
    attributes[SpanAttributes.OUTPUT_MIME_TYPE] = "application/json"
    return attributes


def _get_stream_output_attributes(
    chunks: list, full_content: str = ""
) -> dict[str, AttributeValue]:
    attributes = {}
    messages = {}
    tool_calls = {}
    finishes = {}
    for chunk in chunks:
        # Usage/timing can precede an empty final chunk. Retain the last non-null
        # value of every field instead of consulting only the final chunk.
        attributes.update(_metadata_attributes(chunk))
        for position, choice in enumerate(_get(chunk, "choices") or []):
            index = _get(choice, "index", position)
            message = messages.setdefault(index, {"role": "assistant", "content": ""})
            delta = _get(choice, "delta") or {"content": _get(choice, "text")}
            if role := _get(delta, "role"):
                message["role"] = role
            for field in ("content", "reasoning"):
                if isinstance(value := _get(delta, field), str):
                    message[field] = message.get(field, "") + value
            if finish := _get(choice, "finish_reason"):
                finishes[index] = finish
            calls = tool_calls.setdefault(index, {})
            for position, call in enumerate(_get(delta, "tool_calls") or []):
                call_index = _get(call, "index")
                target = calls.setdefault(
                    call_index if call_index is not None else position, {"function": {}}
                )
                if call_id := _get(call, "id"):
                    target["id"] = call_id
                for field in ("name", "arguments"):
                    if isinstance(value := _get(_get(call, "function"), field), str):
                        target["function"][field] = (
                            target["function"].get(field, "") + value
                        )
    if not messages:
        messages[0] = {"role": "assistant", "content": full_content}
    for index, message in messages.items():
        message["tool_calls"] = [
            value for _, value in sorted(tool_calls.get(index, {}).items())
        ]
        attributes.update(
            _message_attributes(
                message, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}"
            )
        )
    if finishes:
        attributes[SpanAttributes.GEN_AI_RESPONSE_FINISH_REASONS] = [
            value for _, value in sorted(finishes.items())
        ]
    attributes[SpanAttributes.OUTPUT_VALUE] = safe_json_dumps(
        [
            c.model_dump()
            if hasattr(c, "model_dump")
            else c
            if isinstance(c, Mapping)
            else str(c)
            for c in chunks
        ]
    )
    attributes[SpanAttributes.OUTPUT_MIME_TYPE] = "application/json"
    return attributes


def _finish(
    span: Span,
    attributes: Callable[[], dict[str, AttributeValue]],
    error: BaseException | None = None,
) -> None:
    try:
        span.set_attributes(attributes())
    except Exception as extraction_error:  # noqa: BLE001 - telemetry must fail open
        # SDK exception messages may contain customer payloads; log only the type.
        logger.debug(
            "Unable to extract Cerebras response attributes (%s)",
            type(extraction_error).__name__,
        )
    try:
        if error is None:
            span.set_status(Status(StatusCode.OK))
        else:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
    finally:
        span.end()


class _StreamState:
    def __init__(self, span: Span) -> None:
        self.span = span
        self.chunks = []
        self.ended = False

    def finish(self, error: BaseException | None = None) -> None:
        if not self.ended:
            self.ended = True
            _finish(
                self.span, lambda: _get_stream_output_attributes(self.chunks), error
            )
            self.chunks.clear()


class _Stream(ObjectProxy):
    """Keep close(), context management and response access, including before iteration."""

    def __init__(self, stream: Any, span: Span) -> None:
        super().__init__(stream)
        self._self_state = _StreamState(span)
        self._self_iterator = iter(stream)

    def __iter__(self):
        return self

    def __next__(self):
        if self._self_state.ended:
            raise StopIteration
        try:
            with trace_api.use_span(
                self._self_state.span,
                end_on_exit=False,
                record_exception=False,
                set_status_on_exception=False,
            ):
                chunk = next(self._self_iterator)
        except StopIteration:
            self.close()
            raise
        except BaseException as error:
            self._self_state.finish(error)
            try:
                self.close()
            except BaseException as cleanup_error:  # noqa: BLE001 - retain original failure
                # Preserve the iteration failure, not a secondary cleanup error.
                logger.debug(
                    "Unable to close failed Cerebras stream (%s)",
                    type(cleanup_error).__name__,
                )
            raise
        self._self_state.chunks.append(chunk)
        return chunk

    def __enter__(self):
        self.__wrapped__.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            return self.__wrapped__.__exit__(exc_type, exc, traceback)
        except BaseException as error:
            self._self_state.finish(error)
            raise
        finally:
            self._self_state.finish(exc)

    def close(self):
        try:
            if hasattr(self.__wrapped__, "close"):
                return self.__wrapped__.close()
        except BaseException as error:
            self._self_state.finish(error)
            raise
        finally:
            self._self_state.finish()


class _AsyncStream(ObjectProxy):
    def __init__(self, stream: Any, span: Span) -> None:
        super().__init__(stream)
        self._self_state = _StreamState(span)
        self._self_iterator = stream.__aiter__()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._self_state.ended:
            raise StopAsyncIteration
        try:
            with trace_api.use_span(
                self._self_state.span,
                end_on_exit=False,
                record_exception=False,
                set_status_on_exception=False,
            ):
                chunk = await self._self_iterator.__anext__()
        except StopAsyncIteration:
            await self.close()
            raise
        except BaseException as error:
            self._self_state.finish(error)
            try:
                await self.close()
            except BaseException as cleanup_error:  # noqa: BLE001 - retain original failure
                logger.debug(
                    "Unable to close failed Cerebras stream (%s)",
                    type(cleanup_error).__name__,
                )
            raise
        self._self_state.chunks.append(chunk)
        return chunk

    async def __aenter__(self):
        await self.__wrapped__.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            return await self.__wrapped__.__aexit__(exc_type, exc, traceback)
        except BaseException as error:
            self._self_state.finish(error)
            raise
        finally:
            self._self_state.finish(exc)

    async def close(self):
        try:
            # Native AsyncStream uses close(); async generators use aclose().
            close = getattr(self.__wrapped__, "close", None) or getattr(
                self.__wrapped__, "aclose", None
            )
            if close:
                return await close()
        except BaseException as error:
            self._self_state.finish(error)
            raise
        finally:
            self._self_state.finish()

    async def aclose(self):
        return await self.close()


class _WithTracer:
    def __init__(self, tracer: trace_api.Tracer, text: bool = False) -> None:
        self._tracer = tracer
        self._span_name = (
            "Cerebras Text Completions" if text else "Cerebras Chat Completions"
        )

    @contextmanager
    def _start_span(self, attributes: dict[str, AttributeValue]) -> Iterator[Span]:
        try:
            span = self._tracer.start_span(name=self._span_name, attributes=attributes)
        except Exception as error:  # noqa: BLE001 - telemetry must fail open
            logger.debug("Unable to start Cerebras span (%s)", type(error).__name__)
            span = INVALID_SPAN
        with trace_api.use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ):
            yield span

    def _prepare(
        self, kwargs: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, AttributeValue]]:
        kwargs = dict(kwargs)
        # The SDK accepts one-shot iterables. Snapshot them once and pass that
        # same snapshot on; tracing must never drain the customer's request.
        for key in ("messages", "tools", "prompt"):
            if isinstance(kwargs.get(key), IteratorABC):
                kwargs[key] = list(kwargs[key])
        if isinstance(kwargs.get("messages"), (list, tuple)):
            kwargs["messages"] = [
                {**message, "tool_calls": list(message["tool_calls"])}
                if isinstance(message, Mapping)
                and isinstance(message.get("tool_calls"), IteratorABC)
                else message
                for message in kwargs["messages"]
            ]
        try:
            attributes = _get_input_attributes(
                **{key: value for key, value in kwargs.items() if key != "stream"}
            )
        except Exception as error:  # noqa: BLE001 - telemetry must fail open
            logger.debug(
                "Unable to extract Cerebras request attributes (%s)",
                type(error).__name__,
            )
            attributes = {}
        return kwargs, attributes


class _CompletionsWrapper(_WithTracer):
    def __call__(
        self, wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        kwargs, attributes = self._prepare(kwargs)
        with self._start_span(attributes) as span:
            try:
                response = wrapped(*args, **kwargs)
            except BaseException as error:
                _finish(span, dict, error)
                raise
            if kwargs.get("stream") and hasattr(response, "__next__"):
                return _Stream(response, span)
            _finish(span, lambda: _get_output_attributes(response))
            return response


class _AsyncCompletionsWrapper(_WithTracer):
    async def __call__(
        self, wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)
        kwargs, attributes = self._prepare(kwargs)
        with self._start_span(attributes) as span:
            try:
                response = await wrapped(*args, **kwargs)
            except BaseException as error:
                _finish(span, dict, error)
                raise
            if kwargs.get("stream") and hasattr(response, "__anext__"):
                return _AsyncStream(response, span)
            _finish(span, lambda: _get_output_attributes(response))
            return response
