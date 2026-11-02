"""Wrapper classes for vLLM instrumentation."""

import logging
import sys
from abc import ABC
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fi_instrumentation import TraceConfig, safe_json_dumps
from fi_instrumentation.fi_types import (
    FiSpanKindValues,
    ImageAttributes,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
)
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN, Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue
from wrapt import ObjectProxy

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# SDK transport options: request headers and query strings can carry
# credentials, and timeouts are client settings rather than request content.
_TRANSPORT_OPTIONS = frozenset({"extra_headers", "extra_query", "timeout"})


def _shared_instrumentor_enabled() -> bool:
    """Defer to the shared request-level instrumentor while it is enabled.

    Inspect live state, not installation order. Do not import the optional
    package: legacy users can keep OpenAI 1.0, below the shared package's floor.
    This mirrors the Fireworks compatibility route.
    """
    module = sys.modules.get("traceai_openai")
    instrumentor = getattr(module, "OpenAIInstrumentor", None)
    return bool(instrumentor and instrumentor().is_instrumented_by_opentelemetry)


class _WithTracer(ABC):
    """Base class for wrappers that need a tracer."""

    def __init__(
        self,
        tracer: trace_api.Tracer,
        vllm_base_urls: List[str],
        *args: Any,
        config: Optional[TraceConfig] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._tracer = tracer
        self._vllm_base_urls = vllm_base_urls
        self._config = config if config is not None else TraceConfig()

    def _is_vllm_client(self, instance: Any) -> bool:
        """Check if the client instance is configured for vLLM."""
        try:
            if hasattr(instance, "_client"):
                client = instance._client
                if hasattr(client, "base_url"):
                    base_url = str(client.base_url)
                    for vllm_url in self._vllm_base_urls:
                        if vllm_url in base_url:
                            return True
        except Exception:
            pass
        return False

    @contextmanager
    def _start_span(
        self,
        span_name: str,
        attributes: Dict[str, AttributeValue],
    ) -> Iterator[Span]:
        """Start a span with the given name and attributes."""
        try:
            span = self._tracer.start_span(name=span_name, attributes=attributes)
        except Exception:
            span = INVALID_SPAN

        with trace_api.use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            yield span


def _get_input_attributes(
    model: str,
    messages: list,
    *,
    hide_invocation_parameters: bool = False,
    **kwargs: Any,
) -> Dict[str, AttributeValue]:
    """Extract input attributes from request parameters."""
    attributes: Dict[str, AttributeValue] = {
        SpanAttributes.GEN_AI_SPAN_KIND: FiSpanKindValues.LLM.value,
        SpanAttributes.GEN_AI_PROVIDER_NAME: "vllm",
        SpanAttributes.GEN_AI_REQUEST_MODEL: model,
    }

    # Add invocation parameters
    invocation_params = {}
    if "max_tokens" in kwargs and kwargs["max_tokens"] is not None:
        invocation_params["max_tokens"] = kwargs["max_tokens"]
    if "temperature" in kwargs and kwargs["temperature"] is not None:
        invocation_params["temperature"] = kwargs["temperature"]
    if "top_p" in kwargs and kwargs["top_p"] is not None:
        invocation_params["top_p"] = kwargs["top_p"]
    if "stop" in kwargs and kwargs["stop"] is not None:
        invocation_params["stop"] = kwargs["stop"]
    if "frequency_penalty" in kwargs and kwargs["frequency_penalty"] is not None:
        invocation_params["frequency_penalty"] = kwargs["frequency_penalty"]
    if "presence_penalty" in kwargs and kwargs["presence_penalty"] is not None:
        invocation_params["presence_penalty"] = kwargs["presence_penalty"]
    # vLLM specific parameters
    if "best_of" in kwargs and kwargs["best_of"] is not None:
        invocation_params["best_of"] = kwargs["best_of"]
    if "use_beam_search" in kwargs and kwargs["use_beam_search"] is not None:
        invocation_params["use_beam_search"] = kwargs["use_beam_search"]

    if invocation_params:
        attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS] = safe_json_dumps(
            invocation_params
        )

    # Preserve typed message parts: stringifying a multimodal list would put
    # image URLs in message.content, bypassing the image-specific hide policy.
    for i, message in enumerate(messages):
        prefix = f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}"
        role = (
            message.get("role")
            if isinstance(message, dict)
            else getattr(message, "role", None)
        )
        content = (
            message.get("content")
            if isinstance(message, dict)
            else getattr(message, "content", None)
        )
        if role is not None:
            attributes[f"{prefix}.{MessageAttributes.MESSAGE_ROLE}"] = role
        if isinstance(content, str):
            attributes[f"{prefix}.{MessageAttributes.MESSAGE_CONTENT}"] = content
        elif isinstance(content, (list, tuple)):
            for j, part in enumerate(content):
                if not isinstance(part, dict):
                    continue
                part_prefix = f"{prefix}.{MessageAttributes.MESSAGE_CONTENTS}.{j}"
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    attributes[
                        f"{part_prefix}.{MessageContentAttributes.MESSAGE_CONTENT_TEXT}"
                    ] = part["text"]
                elif part.get("type") == "image_url":
                    image = part.get("image_url")
                    if isinstance(image, dict) and isinstance(image.get("url"), str):
                        attributes[
                            f"{part_prefix}.{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}.{ImageAttributes.IMAGE_URL}"
                        ] = image["url"]

    # Keep messages visible, but do not duplicate private invocation settings
    # (including provider-specific extra_body fields) into the raw input.
    raw_input = {"model": model, "messages": messages}
    raw_input.update(
        {
            k: v
            for k, v in kwargs.items()
            if v is not None
            and k not in _TRANSPORT_OPTIONS
            and not hide_invocation_parameters
        }
    )
    attributes[SpanAttributes.INPUT_VALUE] = safe_json_dumps(raw_input)

    return attributes


def _get_output_attributes(response: Any) -> Dict[str, AttributeValue]:
    """Extract output attributes from response."""
    attributes: Dict[str, AttributeValue] = {}

    # Get model from response
    if hasattr(response, "model"):
        attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] = response.model

    # Get choices/messages
    if hasattr(response, "choices") and response.choices:
        choice = response.choices[0]
        prefix = f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0"

        if hasattr(choice, "message"):
            message = choice.message
            if hasattr(message, "role"):
                attributes[f"{prefix}.{MessageAttributes.MESSAGE_ROLE}"] = message.role
            if hasattr(message, "content") and message.content:
                attributes[f"{prefix}.{MessageAttributes.MESSAGE_CONTENT}"] = (
                    message.content
                )
                attributes[SpanAttributes.OUTPUT_VALUE] = message.content

    # Get usage metrics
    if hasattr(response, "usage") and response.usage:
        usage = response.usage
        if hasattr(usage, "prompt_tokens"):
            attributes[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] = usage.prompt_tokens
        if hasattr(usage, "completion_tokens"):
            attributes[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] = (
                usage.completion_tokens
            )
        if hasattr(usage, "total_tokens"):
            attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] = usage.total_tokens

    # Add raw output. Only serialize response models: other objects, such as
    # with_raw_response wrappers, hold the client and its request headers.
    try:
        if hasattr(response, "model_dump"):
            attributes[SpanAttributes.OUTPUT_VALUE] = safe_json_dumps(
                response.model_dump()
            )
    except Exception:
        pass

    return attributes


def _get_stream_output_attributes(
    chunks: list,
    full_content: str,
) -> Dict[str, AttributeValue]:
    """Extract output attributes from streaming response chunks."""
    attributes: Dict[str, AttributeValue] = {
        SpanAttributes.OUTPUT_VALUE: full_content,
        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}": "assistant",
        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}": full_content,
    }

    # Get info from last chunk
    if chunks:
        last_chunk = chunks[-1]
        if hasattr(last_chunk, "model"):
            attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] = last_chunk.model

        if hasattr(last_chunk, "usage") and last_chunk.usage:
            usage = last_chunk.usage
            if hasattr(usage, "prompt_tokens"):
                attributes[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] = (
                    usage.prompt_tokens
                )
            if hasattr(usage, "completion_tokens"):
                attributes[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] = (
                    usage.completion_tokens
                )
            if hasattr(usage, "total_tokens"):
                attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] = (
                    usage.total_tokens
                )

    # Add raw output
    try:
        attributes[SpanAttributes.OUTPUT_VALUE] = safe_json_dumps(
            [c.model_dump() if hasattr(c, "model_dump") else str(c) for c in chunks]
        )
    except Exception:
        pass

    return attributes


def _error_status(error: BaseException) -> Status:
    return Status(StatusCode.ERROR, f"{type(error).__name__}: {error}")


def _end_with_error(span: Span, error: BaseException) -> None:
    span.record_exception(error)
    span.set_status(_error_status(error))
    span.end()


class _StreamSpan:
    """Accumulate chunks and end the span exactly once."""

    __slots__ = ("span", "chunks", "content", "finished")

    def __init__(self, span: Span) -> None:
        self.span = span
        self.chunks: List[Any] = []
        self.content = ""
        self.finished = False

    def process(self, chunk: Any) -> None:
        try:
            self.chunks.append(chunk)
            if hasattr(chunk, "choices") and chunk.choices:
                choice = chunk.choices[0]
                if hasattr(choice, "delta") and hasattr(choice.delta, "content"):
                    if choice.delta.content:
                        self.content += choice.delta.content
        except Exception:
            logger.exception("Failed to accumulate vLLM stream chunk")

    def finish(self, error: Optional[BaseException] = None) -> None:
        if self.finished:
            return
        self.finished = True
        try:
            self.span.set_attributes(
                _get_stream_output_attributes(self.chunks, self.content)
            )
        except Exception:
            logger.exception("Failed to set vLLM stream attributes")
        if error is None:
            self.span.set_status(Status(StatusCode.OK))
            self.span.end()
        else:
            _end_with_error(self.span, error)


class _Stream(ObjectProxy):  # type: ignore[misc]
    """Proxy the SDK Stream so its API stays intact while chunks are traced."""

    __slots__ = ("_self_tracking",)

    def __init__(self, stream: Any, span: Span) -> None:
        super().__init__(stream)
        self._self_tracking = _StreamSpan(span)

    def __iter__(self) -> "_Stream":
        return self

    def __next__(self) -> Any:
        try:
            chunk = self.__wrapped__.__next__()
        except StopIteration:
            self._self_tracking.finish()
            raise
        except BaseException as error:
            self._self_tracking.finish(error)
            raise
        self._self_tracking.process(chunk)
        return chunk

    def __enter__(self) -> "_Stream":
        # ObjectProxy would return the SDK stream and bypass this iterator.
        self.__wrapped__.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return self.__wrapped__.__exit__(exc_type, exc, tb)
        except BaseException as close_error:
            exc = close_error
            raise
        finally:
            self._self_tracking.finish(exc)

    def close(self) -> None:
        try:
            close = getattr(self.__wrapped__, "close", None)
            if close is not None:
                close()
            else:
                # OpenAI 1.0 streams expose the response but not close().
                self.__wrapped__.response.close()
        except BaseException as error:
            self._self_tracking.finish(error)
            raise
        self._self_tracking.finish()


class _AsyncStream(ObjectProxy):  # type: ignore[misc]
    """Proxy the SDK AsyncStream so its API stays intact while chunks are traced."""

    __slots__ = ("_self_tracking",)

    def __init__(self, stream: Any, span: Span) -> None:
        super().__init__(stream)
        self._self_tracking = _StreamSpan(span)

    def __aiter__(self) -> "_AsyncStream":
        return self

    async def __anext__(self) -> Any:
        try:
            chunk = await self.__wrapped__.__anext__()
        except StopAsyncIteration:
            self._self_tracking.finish()
            raise
        except BaseException as error:
            self._self_tracking.finish(error)
            raise
        self._self_tracking.process(chunk)
        return chunk

    async def __aenter__(self) -> "_AsyncStream":
        await self.__wrapped__.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return await self.__wrapped__.__aexit__(exc_type, exc, tb)
        except BaseException as close_error:
            exc = close_error
            raise
        finally:
            self._self_tracking.finish(exc)

    async def close(self) -> None:
        try:
            close = getattr(self.__wrapped__, "close", None)
            if close is not None:
                await close()
            else:
                await self.__wrapped__.response.aclose()
        except BaseException as error:
            self._self_tracking.finish(error)
            raise
        self._self_tracking.finish()

    async def aclose(self) -> None:
        # ObjectProxy otherwise delegates the bound SDK alias past our close().
        await self.close()


def _finish_response(span: Span, response: Any) -> None:
    """End the span for a non-stream result.

    A with_raw_response stream also lands here: it is returned unchanged so the
    SDK raw-response API keeps working, and its chunks are not recorded.
    """
    try:
        span.set_attributes(_get_output_attributes(response))
    except Exception:
        logger.exception("Failed to set vLLM response attributes")
    span.set_status(Status(StatusCode.OK))
    span.end()


class _CompletionsWrapper(_WithTracer):
    """Wrapper for synchronous chat completions."""

    def __call__(
        self,
        wrapped: Any,
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> Any:
        # Check if this is a vLLM client
        if _shared_instrumentor_enabled() or not self._is_vllm_client(instance):
            return wrapped(*args, **kwargs)

        # Extract parameters
        model = kwargs.get("model", args[0] if args else "unknown")
        messages = kwargs.get("messages", args[1] if len(args) > 1 else [])
        if isinstance(messages, Iterable) and not isinstance(
            messages, (list, tuple, str, bytes)
        ):
            # Trace and send the same materialized input, never exhaust the
            # caller's single-use iterable solely for instrumentation.
            messages = list(messages)
            if "messages" in kwargs:
                kwargs = {**kwargs, "messages": messages}
            else:
                args = (args[0], messages, *args[2:])
        stream = kwargs.get("stream", False)

        # Get input attributes - exclude model, messages, stream from kwargs
        extra_kwargs = {
            k: v for k, v in kwargs.items() if k not in ("model", "messages", "stream")
        }
        input_attrs = _get_input_attributes(
            model,
            messages,
            hide_invocation_parameters=self._config.hide_llm_invocation_parameters,
            **extra_kwargs,
        )

        with self._start_span("vLLM Chat Completions", input_attrs) as span:
            try:
                response = wrapped(*args, **kwargs)
            except BaseException as e:
                # Includes KeyboardInterrupt and other non-Exception interrupts.
                _end_with_error(span, e)
                raise

            if stream and hasattr(response, "__next__"):
                return _Stream(response, span)
            _finish_response(span, response)
            return response


class _AsyncCompletionsWrapper(_WithTracer):
    """Wrapper for asynchronous chat completions."""

    async def __call__(
        self,
        wrapped: Any,
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> Any:
        # Check if this is a vLLM client
        if _shared_instrumentor_enabled() or not self._is_vllm_client(instance):
            return await wrapped(*args, **kwargs)

        # Extract parameters
        model = kwargs.get("model", args[0] if args else "unknown")
        messages = kwargs.get("messages", args[1] if len(args) > 1 else [])
        if isinstance(messages, Iterable) and not isinstance(
            messages, (list, tuple, str, bytes)
        ):
            # Trace and send the same materialized input, never exhaust the
            # caller's single-use iterable solely for instrumentation.
            messages = list(messages)
            if "messages" in kwargs:
                kwargs = {**kwargs, "messages": messages}
            else:
                args = (args[0], messages, *args[2:])
        stream = kwargs.get("stream", False)

        # Get input attributes - exclude model, messages, stream from kwargs
        extra_kwargs = {
            k: v for k, v in kwargs.items() if k not in ("model", "messages", "stream")
        }
        input_attrs = _get_input_attributes(
            model,
            messages,
            hide_invocation_parameters=self._config.hide_llm_invocation_parameters,
            **extra_kwargs,
        )

        with self._start_span("vLLM Chat Completions", input_attrs) as span:
            try:
                response = await wrapped(*args, **kwargs)
            except BaseException as e:
                # Includes asyncio.CancelledError from timeouts and task cancellation.
                _end_with_error(span, e)
                raise

            if stream and hasattr(response, "__anext__"):
                return _AsyncStream(response, span)
            _finish_response(span, response)
            return response
