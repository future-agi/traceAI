"""Wrapper classes for Fireworks AI instrumentation."""

import logging
import sys
from abc import ABC
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import urlsplit

from fi_instrumentation import TraceConfig, safe_json_dumps
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes, MessageAttributes
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN, Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from traceai_fireworks._privacy import _messages_for_telemetry
from traceai_fireworks._raw_response import _RawStreamResponse
from traceai_fireworks._stream import (
    _AsyncStream,
    _Stream,
    _get_stream_output_attributes as _get_stream_output_attributes,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Fireworks-specific base URL for detection
FIREWORKS_BASE_URL = "api.fireworks.ai"

# Capture model request data, never SDK transport options. Arbitrary extra_body,
# extra_headers, extra_query and future unknown kwargs may contain credentials.
_REQUEST_BODY_FIELDS = frozenset({
    "audio", "frequency_penalty", "function_call", "functions", "logit_bias",
    "logprobs", "max_completion_tokens", "max_tokens", "modalities", "n",
    "parallel_tool_calls", "prediction", "presence_penalty", "reasoning_effort",
    "response_format", "seed", "service_tier", "stop", "store", "stream_options",
    "temperature", "tool_choice", "tools", "top_logprobs", "top_p", "user",
})


class _WithTracer(ABC):
    """Base class for wrappers that need a tracer."""

    def __init__(
        self, tracer: trace_api.Tracer, *args: Any,
        config: Optional[TraceConfig] = None, **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._tracer = tracer
        self._config = config if config is not None else TraceConfig()

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


def _is_fireworks_client(instance: Any) -> bool:
    """Check if the client instance is configured for Fireworks AI."""
    try:
        if hasattr(instance, "_client"):
            client = instance._client
            if hasattr(client, "base_url"):
                host = urlsplit(str(client.base_url)).hostname
                return bool(host and host.lower().rstrip(".") == FIREWORKS_BASE_URL)
    except Exception:
        pass
    return False


def _shared_instrumentor_enabled() -> bool:
    """Let the request-level instrumentor own spans when both routes are enabled.

    Do not import the optional package here: legacy users can keep openai 1.0,
    which predates the shared instrumentor's supported SDK floor. Inspect live
    state on each call so either installation/uninstallation order works.
    """
    module = sys.modules.get("traceai_openai")
    instrumentor = getattr(module, "OpenAIInstrumentor", None)
    return bool(instrumentor and instrumentor().is_instrumented_by_opentelemetry)


def _get_input_attributes(
    model: str,
    messages: list,
    *,
    config: Optional[TraceConfig] = None,
    **kwargs: Any,
) -> Dict[str, AttributeValue]:
    """Extract input attributes from request parameters."""
    attributes: Dict[str, AttributeValue] = {
        SpanAttributes.GEN_AI_SPAN_KIND: FiSpanKindValues.LLM.value,
        SpanAttributes.GEN_AI_PROVIDER_NAME: "fireworks",
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

    if invocation_params:
        attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS] = safe_json_dumps(invocation_params)

    # Mask images before BOTH flattened content and raw-input serialization.
    # Never replace or mutate the messages passed to the provider.
    messages = _messages_for_telemetry(messages, config if config is not None else TraceConfig())

    # Add input messages from the same privacy-filtered copy as raw input.
    for i, message in enumerate(messages):
        prefix = f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}"
        if "role" in message:
            attributes[f"{prefix}.{MessageAttributes.MESSAGE_ROLE}"] = message["role"]
        if "content" in message:
            content = message["content"]
            attributes[f"{prefix}.{MessageAttributes.MESSAGE_CONTENT}"] = str(content) if content else ""

    # Only allow known model request fields into telemetry.
    raw_input = {"model": model, "messages": messages}
    raw_input.update({
        k: v for k, v in kwargs.items() if k in _REQUEST_BODY_FIELDS and v is not None
    })
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


class _CompletionsWrapper(_WithTracer):
    """Wrapper for synchronous chat completions."""

    def __call__(
        self,
        wrapped: Any,
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> Any:
        # One request must have one owner, regardless of setup order.
        if _shared_instrumentor_enabled() or not _is_fireworks_client(instance):
            return wrapped(*args, **kwargs)

        # Extract parameters
        model = kwargs.get("model", args[0] if args else "unknown")
        messages = kwargs.get("messages", args[1] if len(args) > 1 else [])
        stream = kwargs.get("stream", False)

        # Get input attributes - exclude model, messages, stream from kwargs
        extra_kwargs = {k: v for k, v in kwargs.items() if k not in ("model", "messages", "stream")}
        input_attrs = _get_input_attributes(model, messages, config=self._config, **extra_kwargs)

        with self._start_span("Fireworks Chat Completions", input_attrs) as span:
            try:
                response = wrapped(*args, **kwargs)

                if stream:
                    return self._wrap_stream(response, span)

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

    def _wrap_stream(self, stream: Any, span: Span) -> Any:
        """Transfer span ownership to a proxy, including before first iteration."""
        if callable(getattr(type(stream), "parse", None)):
            return _RawStreamResponse(stream, span, asynchronous=False)
        return _Stream(stream, span)


class _AsyncCompletionsWrapper(_WithTracer):
    """Wrapper for asynchronous chat completions."""

    async def __call__(
        self,
        wrapped: Any,
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> Any:
        # One request must have one owner, regardless of setup order.
        if _shared_instrumentor_enabled() or not _is_fireworks_client(instance):
            return await wrapped(*args, **kwargs)

        # Extract parameters
        model = kwargs.get("model", args[0] if args else "unknown")
        messages = kwargs.get("messages", args[1] if len(args) > 1 else [])
        stream = kwargs.get("stream", False)

        # Get input attributes - exclude model, messages, stream from kwargs
        extra_kwargs = {k: v for k, v in kwargs.items() if k not in ("model", "messages", "stream")}
        input_attrs = _get_input_attributes(model, messages, config=self._config, **extra_kwargs)

        with self._start_span("Fireworks Chat Completions", input_attrs) as span:
            try:
                response = await wrapped(*args, **kwargs)

                if stream:
                    return self._wrap_async_stream(response, span)

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

    def _wrap_async_stream(self, stream: Any, span: Span) -> Any:
        """Transfer ownership only after the SDK returns a response."""
        if callable(getattr(type(stream), "parse", None)):
            return _RawStreamResponse(stream, span, asynchronous=True)
        return _AsyncStream(stream, span)
