"""Wrapper classes for vLLM instrumentation."""

import logging
import sys
from abc import ABC
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes, MessageAttributes
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN, Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue
from wrapt import ObjectProxy

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


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
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._tracer = tracer
        self._vllm_base_urls = vllm_base_urls

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
        attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS] = safe_json_dumps(invocation_params)

    # Add input messages
    for i, message in enumerate(messages):
        prefix = f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}"
        if isinstance(message, dict):
            if "role" in message:
                attributes[f"{prefix}.{MessageAttributes.MESSAGE_ROLE}"] = message["role"]
            if "content" in message:
                attributes[f"{prefix}.{MessageAttributes.MESSAGE_CONTENT}"] = str(message["content"]) if message["content"] else ""
        else:
            # Handle message objects
            if hasattr(message, "role"):
                attributes[f"{prefix}.{MessageAttributes.MESSAGE_ROLE}"] = message.role
            if hasattr(message, "content"):
                attributes[f"{prefix}.{MessageAttributes.MESSAGE_CONTENT}"] = str(message.content) if message.content else ""

    # Project semantic fields only. Transport options (headers, query, body,
    # timeout and future SDK kwargs) must never enter telemetry. The original
    # kwargs are still passed unchanged to the SDK.
    raw_input = {"model": model, "messages": messages, **invocation_params}
    for key in ("tools", "tool_choice", "response_format", "max_completion_tokens", "n", "seed"):
        if key in kwargs and kwargs[key] is not None:
            raw_input[key] = kwargs[key]
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
                attributes[f"{prefix}.{MessageAttributes.MESSAGE_CONTENT}"] = message.content
                attributes[SpanAttributes.OUTPUT_VALUE] = message.content

    # Get usage metrics
    if hasattr(response, "usage") and response.usage:
        usage = response.usage
        if hasattr(usage, "prompt_tokens"):
            attributes[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] = usage.prompt_tokens
        if hasattr(usage, "completion_tokens"):
            attributes[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] = usage.completion_tokens
        if hasattr(usage, "total_tokens"):
            attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] = usage.total_tokens

    # Add raw output
    try:
        if hasattr(response, "model_dump"):
            attributes[SpanAttributes.OUTPUT_VALUE] = safe_json_dumps(response.model_dump())
        elif hasattr(response, "__dict__"):
            attributes[SpanAttributes.OUTPUT_VALUE] = safe_json_dumps(response.__dict__)
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
                attributes[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] = usage.prompt_tokens
            if hasattr(usage, "completion_tokens"):
                attributes[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] = usage.completion_tokens
            if hasattr(usage, "total_tokens"):
                attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] = usage.total_tokens

    # Add raw output
    try:
        attributes[SpanAttributes.OUTPUT_VALUE] = safe_json_dumps([
            c.model_dump() if hasattr(c, "model_dump") else str(c) for c in chunks
        ])
    except Exception:
        pass

    return attributes


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
        stream = kwargs.get("stream", False)

        # Get input attributes - exclude model, messages, stream from kwargs
        extra_kwargs = {k: v for k, v in kwargs.items() if k not in ("model", "messages", "stream")}
        input_attrs = _get_input_attributes(model, messages, **extra_kwargs)

        with self._start_span("vLLM Chat Completions", input_attrs) as span:
            try:
                response = wrapped(*args, **kwargs)

                if stream:
                    return _Stream(response, span)

                # Non-streaming response
                output_attrs = _get_output_attributes(response)
                span.set_attributes(output_attrs)
                span.set_status(Status(StatusCode.OK))
                span.end()
                return response

            except BaseException as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.end()
                raise


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
        stream = kwargs.get("stream", False)

        # Get input attributes - exclude model, messages, stream from kwargs
        extra_kwargs = {k: v for k, v in kwargs.items() if k not in ("model", "messages", "stream")}
        input_attrs = _get_input_attributes(model, messages, **extra_kwargs)

        with self._start_span("vLLM Chat Completions", input_attrs) as span:
            try:
                response = await wrapped(*args, **kwargs)

                if stream:
                    return _AsyncStream(response, span)

                # Non-streaming response
                output_attrs = _get_output_attributes(response)
                span.set_attributes(output_attrs)
                span.set_status(Status(StatusCode.OK))
                span.end()
                return response

            except BaseException as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.end()
                raise


class _StreamState(ObjectProxy):
    """Observe the caller's stream without replacing its SDK interfaces."""

    def __init__(self, stream: Any, span: Span) -> None:
        super().__init__(stream)
        self._self_span = span
        self._self_finished = False
        self._self_chunks = []
        self._self_content = []
        self._self_iterator = None

    def _observe(self, chunk: Any) -> Any:
        if not self._self_finished:
            self._self_chunks.append(chunk)
            if getattr(chunk, "choices", None):
                content = getattr(getattr(chunk.choices[0], "delta", None), "content", None)
                if content:
                    self._self_content.append(content)
        return chunk

    def _finish(self, error: Optional[BaseException] = None) -> None:
        if self._self_finished:
            return
        self._self_finished = True
        span = self._self_span
        try:
            span.set_attributes(_get_stream_output_attributes(
                self._self_chunks, "".join(self._self_content)
            ))
            if error is not None:
                # FITracer applies error-detail privacy to both surfaces.
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, str(error)))
            else:
                span.set_status(Status(StatusCode.OK))
        except Exception:
            logger.warning("Failed to collect vLLM stream telemetry")
        finally:
            self._self_chunks.clear()
            self._self_content.clear()
            span.end()


class _Stream(_StreamState):
    def __iter__(self) -> Any:
        return self

    def __next__(self) -> Any:
        try:
            if self._self_iterator is None:
                self._self_iterator = iter(self.__wrapped__)
            with trace_api.use_span(self._self_span, end_on_exit=False,
                                    record_exception=False, set_status_on_exception=False):
                chunk = next(self._self_iterator)
        except StopIteration:
            self._finish()
            raise
        except BaseException as error:
            self._finish(error)
            raise
        return self._observe(chunk)

    def __enter__(self) -> Any:
        try:
            enter = getattr(self.__wrapped__, "__enter__", None)
            if enter is not None:
                enter()
        except BaseException as error:
            self._finish(error)
            raise
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        try:
            exit_stream = getattr(self.__wrapped__, "__exit__", None)
            if exit_stream is not None:
                return exit_stream(exc_type, exc_value, traceback)
            return self.__wrapped__.response.close()
        except BaseException as error:
            self._finish(error)
            raise
        finally:
            self._finish(exc_value)

    def close(self) -> Any:
        try:
            # OpenAI 1.0 exposes only response.close(), not Stream.close().
            close = getattr(self.__wrapped__, "close", None) or self.__wrapped__.response.close
            return close()
        except BaseException as error:
            self._finish(error)
            raise
        finally:
            self._finish()


class _AsyncStream(_StreamState):
    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> Any:
        try:
            if self._self_iterator is None:
                self._self_iterator = self.__wrapped__.__aiter__()
            with trace_api.use_span(self._self_span, end_on_exit=False,
                                    record_exception=False, set_status_on_exception=False):
                chunk = await self._self_iterator.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as error:
            self._finish(error)
            raise
        return self._observe(chunk)

    async def __aenter__(self) -> Any:
        try:
            enter = getattr(self.__wrapped__, "__aenter__", None)
            if enter is not None:
                await enter()
        except BaseException as error:
            self._finish(error)
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        try:
            exit_stream = getattr(self.__wrapped__, "__aexit__", None)
            if exit_stream is not None:
                return await exit_stream(exc_type, exc_value, traceback)
            return await self.__wrapped__.response.aclose()
        except BaseException as error:
            self._finish(error)
            raise
        finally:
            self._finish(exc_value)

    async def close(self) -> Any:
        try:
            close = getattr(self.__wrapped__, "close", None) or self.__wrapped__.response.aclose
            return await close()
        except BaseException as error:
            self._finish(error)
            raise
        finally:
            self._finish()
