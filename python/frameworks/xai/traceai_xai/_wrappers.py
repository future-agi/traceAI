"""Wrapper classes for xAI (Grok) instrumentation."""

import logging
import sys
from abc import ABC
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes, MessageAttributes
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN, Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# xAI-specific base URL for detection
XAI_BASE_URL = "api.x.ai"
# Per-call capture policy key, shared by name (fi_instrumentation.attachments.media_capture).
_CAPTURE_POLICY_KEY = "traceai.auto.attachment_policy.v1"


class _WithTracer(ABC):
    """Base class for wrappers that need a tracer."""

    _media_dialect = "image"

    def __init__(
        self,
        tracer: trace_api.Tracer,
        *args: Any,
        attachments: Any = None,
        config: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._tracer = tracer
        # Provider-owned shared attachment manager, or None (no capture, no imports).
        self._attachments = attachments
        self._config = config

    def _capture_chat_media(self, span: Any, messages: Any) -> None:
        """Typed chat media (data-URI images) through the shared attachment manager; never raises."""
        if self._attachments is None:
            return
        try:
            from fi_instrumentation.attachments.provider_media import ProviderMedia

            ProviderMedia(
                self._attachments, span, "xai", trace_config=self._config
            ).compatible_messages(messages, self._media_dialect)
        except Exception:
            logger.debug("Attachment capture unavailable", exc_info=True)

    def _defers(self) -> bool:
        """Hand the whole call to traceai_openai only while both sides capture attachments.

        With attachments off here this binding traces exactly as before (its own span too).
        With traceai_openai enabled but without a manager, this binding keeps the call and
        captures its media itself.
        """
        return self._attachments is not None and _shared_instrumentor_captures()

    @contextmanager
    def _deferred(self) -> Iterator[None]:
        """Run the handed-over call under this binding's local deny (deny wins downstream)."""
        try:
            from fi_instrumentation.attachments.provider_media import deferred

            token = context_api.attach(deferred(self._config))
        except Exception:
            # An unreadable restriction is a restriction: deny capture for this call.
            token = context_api.attach(context_api.set_value(_CAPTURE_POLICY_KEY, False))
        try:
            yield
        finally:
            context_api.detach(token)

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


def _is_xai_client(instance: Any) -> bool:
    """Check if the client instance is configured for xAI."""
    try:
        if hasattr(instance, "_client"):
            client = instance._client
            if hasattr(client, "base_url"):
                base_url = str(client.base_url)
                return XAI_BASE_URL in base_url
    except Exception:
        pass
    return False


def _shared_instrumentor_captures() -> bool:
    """True while traceai_openai's OpenAIInstrumentor is enabled WITH an active attachment manager.

    The request-level OpenAI instrumentor then owns the call (span and media), whatever
    the setup order. Enabled without a manager, or an older traceai_openai without the
    accessor, means it would capture nothing, so this binding keeps the call. Do not
    import the optional package; inspect live state on each call.
    """
    module = sys.modules.get("traceai_openai")
    instrumentor = getattr(module, "OpenAIInstrumentor", None)
    if instrumentor is None:
        return False
    captures = getattr(instrumentor(), "captures_attachments", None)
    return bool(callable(captures) and captures())


def _get_input_attributes(
    model: str,
    messages: list,
    **kwargs: Any,
) -> Dict[str, AttributeValue]:
    """Extract input attributes from request parameters."""
    attributes: Dict[str, AttributeValue] = {
        SpanAttributes.GEN_AI_SPAN_KIND: FiSpanKindValues.LLM.value,
        SpanAttributes.GEN_AI_PROVIDER_NAME: "xai",
        SpanAttributes.GEN_AI_PROVIDER_NAME: "xai",
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

    # Add raw input
    raw_input = {"model": model, "messages": messages}
    raw_input.update({k: v for k, v in kwargs.items() if v is not None})
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
        # Check if this is an xAI client
        if not _is_xai_client(instance):
            return wrapped(*args, **kwargs)
        # One request must have one owner, regardless of setup order.
        if self._defers():
            with self._deferred():
                return wrapped(*args, **kwargs)

        # Extract parameters
        model = kwargs.get("model", args[0] if args else "unknown")
        messages = kwargs.get("messages", args[1] if len(args) > 1 else [])
        stream = kwargs.get("stream", False)

        # Get input attributes - exclude model, messages, stream from kwargs
        extra_kwargs = {k: v for k, v in kwargs.items() if k not in ("model", "messages", "stream")}
        input_attrs = _get_input_attributes(model, messages, **extra_kwargs)

        with self._start_span("xAI Chat Completions", input_attrs) as span:
            self._capture_chat_media(span, messages)
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

            except Exception as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.end()
                raise

    def _wrap_stream(self, stream: Any, span: Span) -> Any:
        """Wrap a streaming response to capture all chunks."""
        chunks = []
        full_content = ""

        try:
            for chunk in stream:
                chunks.append(chunk)
                if hasattr(chunk, "choices") and chunk.choices:
                    choice = chunk.choices[0]
                    if hasattr(choice, "delta") and hasattr(choice.delta, "content"):
                        if choice.delta.content:
                            full_content += choice.delta.content
                yield chunk

            output_attrs = _get_stream_output_attributes(chunks, full_content)
            span.set_attributes(output_attrs)
            span.set_status(Status(StatusCode.OK))

        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            raise

        finally:
            span.end()


class _AsyncCompletionsWrapper(_WithTracer):
    """Wrapper for asynchronous chat completions."""

    async def __call__(
        self,
        wrapped: Any,
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> Any:
        # Check if this is an xAI client
        if not _is_xai_client(instance):
            return await wrapped(*args, **kwargs)
        # One request must have one owner, regardless of setup order.
        if self._defers():
            with self._deferred():
                return await wrapped(*args, **kwargs)

        # Extract parameters
        model = kwargs.get("model", args[0] if args else "unknown")
        messages = kwargs.get("messages", args[1] if len(args) > 1 else [])
        stream = kwargs.get("stream", False)

        # Get input attributes - exclude model, messages, stream from kwargs
        extra_kwargs = {k: v for k, v in kwargs.items() if k not in ("model", "messages", "stream")}
        input_attrs = _get_input_attributes(model, messages, **extra_kwargs)

        with self._start_span("xAI Chat Completions", input_attrs) as span:
            self._capture_chat_media(span, messages)
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

            except Exception as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.end()
                raise

    async def _wrap_async_stream(self, stream: Any, span: Span) -> Any:
        """Wrap an async streaming response to capture all chunks."""
        chunks = []
        full_content = ""

        try:
            async for chunk in stream:
                chunks.append(chunk)
                if hasattr(chunk, "choices") and chunk.choices:
                    choice = chunk.choices[0]
                    if hasattr(choice, "delta") and hasattr(choice.delta, "content"):
                        if choice.delta.content:
                            full_content += choice.delta.content
                yield chunk

            output_attrs = _get_stream_output_attributes(chunks, full_content)
            span.set_attributes(output_attrs)
            span.set_status(Status(StatusCode.OK))

        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            raise

        finally:
            span.end()
