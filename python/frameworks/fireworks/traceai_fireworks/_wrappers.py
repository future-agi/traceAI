"""Wrapper classes for Fireworks AI instrumentation."""

import inspect
import logging
import sys
from abc import ABC
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import urlsplit

from fi_instrumentation import REDACTED_VALUE, TraceConfig, safe_json_dumps

try:
    from fi_instrumentation.instrumentation.config import ChatInput
except ImportError:
    # The declared published 1.2.2 floor predates the structured-input API and
    # the opaque-input guard. Retain its existing path, not a validation fallback.
    ChatInput = None
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes, MessageAttributes
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN, Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from traceai_fireworks._privacy import _hide_raw_output, _messages_for_telemetry
from traceai_fireworks._raw_response import _RawNonStreamResponse, _RawStreamResponse
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
        # Structured input belongs on the supported FI span setter. A sampler
        # is allowed to return/replace opaque attributes, which core must mask
        # fail-closed again. Never turn its returned JSON into a trusted string.
        structured = attributes.get(SpanAttributes.INPUT_VALUE)
        if ChatInput is not None and type(structured) is ChatInput:
            attributes = {k: v for k, v in attributes.items() if k != SpanAttributes.INPUT_VALUE}
        else:
            structured = None
        try:
            span = self._tracer.start_span(name=span_name, attributes=attributes)
            if structured is not None:
                span.set_attribute(SpanAttributes.INPUT_VALUE, structured)
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


_SDK_PLAIN_NODES = 10000  # same allowance as core's ChatInput copy budget
_type_mro = type.__dict__["__mro__"].__get__
_type_dict = type.__dict__["__dict__"].__get__
_type_name = type.__dict__["__name__"].__get__


class _OverBudget(Exception):
    pass


def _genuine_sdk_model(kind: type, sdk_model: type) -> bool:
    """Classify canonical OpenAI classes without metaclass equality/descriptors."""
    try:
        mro = _type_mro(kind)
        found_base = False
        for base in mro:
            if base is sdk_model:  # identity only; tuple membership may call metaclass __eq__
                found_base = True
                break
            # A subclass override can install its own serializer or metaclass hooks.
            if "model_dump" in _type_dict(base):
                return False
        if not found_base:
            return False
        namespace = _type_dict(kind)
        module_name = namespace.get("__module__")
        # __name__ is stored in type's native slot, not the class namespace.
        class_name = _type_name(kind)
        if (type(module_name) is not str or not module_name.startswith("openai.")
                or type(class_name) is not str):
            return False
        # Accept only the exact class exported by its already-loaded defining module.
        # ModuleType's native dict descriptor and dict.get avoid user attribute hooks.
        from types import ModuleType
        module = __import__("sys").modules.get(module_name)
        if type(module) is not ModuleType:
            return False
        module_dict = ModuleType.__dict__["__dict__"].__get__(module)
        return module_dict.get(class_name) is kind
    except Exception:
        return False


def _sdk_plain(value: Any) -> Any:
    """Copy built-in containers, converting only genuine OpenAI SDK models to plain JSON.

    Mirrors what the SDK sends on the wire: SDK models become JSON and exact-int
    dict keys (e.g. logit_bias token ids) become strings. Everything else,
    including user subclasses of SDK types, is returned unchanged so core's
    strict validator rejects it without evaluating it. Bounded by the same node
    budget as core; past it the original value is returned (and fails closed).
    Caller objects are never modified.
    """
    try:
        from openai._models import BaseModel as sdk_model
    except ImportError:  # pragma: no cover - openai is a hard dependency
        return value
    budget = [_SDK_PLAIN_NODES]

    def walk(item: Any, depth: int) -> Any:
        budget[0] -= 1
        if budget[0] < 0 or depth > 32:
            raise _OverBudget
        kind = type(item)
        if kind is dict:
            copied = {}
            for key, child in item.items():
                key_kind = type(key)
                if key_kind is str:
                    normalized = key
                elif key_kind is int:
                    try:
                        # This is the SDK/JSON wire normalization for integer keys.
                        normalized = str(key)
                    except (ValueError, OverflowError):
                        return item
                else:
                    # Never rehash or stringify a caller-controlled key. Core's
                    # strict validator will fail this original mapping closed.
                    return item
                copied[normalized] = walk(child, depth + 1)
            return copied
        if kind is list or kind is tuple:
            return [walk(child, depth + 1) for child in item]
        if _genuine_sdk_model(kind, sdk_model):
            try:
                # pydantic's own implementation, never an override; warnings off
                # so mismatched values are not written to the warning stream.
                dumped = sdk_model.model_dump(item, mode="json", exclude_unset=True, warnings=False)
            except Exception:
                return item
            return walk(dumped, depth + 1)
        return item

    try:
        return walk(value, 0)
    except (_OverBudget, RecursionError):
        return value


def _get_input_attributes(
    model: str,
    messages: Any,
    *,
    config: Optional[TraceConfig] = None,
    **kwargs: Any,
) -> Dict[str, AttributeValue]:
    """Extract input attributes from request parameters."""
    config = config if config is not None else TraceConfig()
    attributes: Dict[str, AttributeValue] = {
        SpanAttributes.GEN_AI_SPAN_KIND: FiSpanKindValues.LLM.value,
        SpanAttributes.GEN_AI_PROVIDER_NAME: "fireworks",
        SpanAttributes.GEN_AI_REQUEST_MODEL: model,
    }

    # Resolve SDK types only at the request boundary; package import must stay
    # usable without OpenAI. Never infer omission from names, equality or hooks.
    from openai import _types as sdk_types

    omission_types = tuple(
        kind for name in ("NotGiven", "Omit")
        if (kind := getattr(sdk_types, name, None)) is not None
    )
    kwargs = {k: v for k, v in kwargs.items()
              if not any(type(v) is kind for kind in omission_types)}
    if ChatInput is not None and any((config.hide_inputs, config.hide_input_messages,
                                      config.hide_input_text, config.hide_input_images,
                                      config.pii_redaction)):
        # Pass the ORIGINAL typed input, not a previously sanitized string.
        # Core applies policy independently for this copy and at the span setter.
        # SDK models (e.g. a ChatCompletionMessage appended in a tool loop) are
        # converted to the plain JSON the SDK itself sends; core still validates.
        # Hidden inputs are never projected, so skip conversion entirely there.
        convert = (lambda value: value) if config.hide_inputs else _sdk_plain
        raw_input = {"model": model}
        if type(messages) is list or type(messages) is tuple:
            raw_input["messages"] = messages if config.hide_input_messages else convert(messages)
        raw_input.update({k: convert(v) for k, v in kwargs.items() if k in _REQUEST_BODY_FIELDS})
        structured = ChatInput(raw_input)
        attributes[SpanAttributes.INPUT_VALUE] = structured
        telemetry = ChatInput.masked(structured, config)
        invocation_params = None
        if telemetry is not None:
            for i, message in enumerate(telemetry.get("messages", [])):
                prefix = f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}"
                attributes[f"{prefix}.{MessageAttributes.MESSAGE_ROLE}"] = message["role"]
                if "content" in message:
                    content = message["content"]
                    attributes[f"{prefix}.{MessageAttributes.MESSAGE_CONTENT}"] = str(content) if content else ""
            invocation_params = {k: v for k, v in telemetry.items() if k not in ("model", "messages")}
        else:
            # Typed invocation metadata is allowed independently of whether the
            # messages could be projected; input core rejects stays fail-closed.
            invocation_params = ChatInput.masked_invocation_parameters(structured, config)
        if invocation_params:
            attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS] = safe_json_dumps(invocation_params)
        return attributes

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

    if invocation_params and not (config.hide_inputs or config.hide_llm_invocation_parameters):
        attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS] = safe_json_dumps(invocation_params)

    # Apply policy before BOTH flattened content and raw-input serialization.
    # Never replace or mutate the messages passed to the provider.
    messages = _messages_for_telemetry(messages, config)

    # Add input messages from the same privacy-filtered copy as raw input.
    for i, message in enumerate(messages or []):
        prefix = f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}"
        if "role" in message:
            attributes[f"{prefix}.{MessageAttributes.MESSAGE_ROLE}"] = message["role"]
        if "content" in message:
            content = message["content"]
            attributes[f"{prefix}.{MessageAttributes.MESSAGE_CONTENT}"] = str(content) if content else ""

    # Only allow known model request fields into telemetry.
    raw_input: Dict[str, Any] = {"model": model}
    if messages is not None:
        raw_input["messages"] = messages
    if not (config.hide_inputs or config.hide_llm_invocation_parameters):
        raw_input.update({
            k: v for k, v in kwargs.items() if k in _REQUEST_BODY_FIELDS and v is not None
        })
    attributes[SpanAttributes.INPUT_VALUE] = (
        REDACTED_VALUE if config.hide_inputs else safe_json_dumps(raw_input)
    )

    return attributes


def _get_output_attributes(
    response: Any, *, config: Optional[TraceConfig] = None,
) -> Dict[str, AttributeValue]:
    """Extract attributes only from a parsed provider response, never SDK internals."""
    config = config if config is not None else TraceConfig()
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

    # Raw response wrappers carry credentials and request options in __dict__.
    # Only serialize the provider's public model, and cover raw duplicates when
    # a narrower message/text policy is selected.
    try:
        if _hide_raw_output(config):
            attributes[SpanAttributes.OUTPUT_VALUE] = REDACTED_VALUE
        elif callable(getattr(response, "model_dump", None)):
            attributes[SpanAttributes.OUTPUT_VALUE] = safe_json_dumps(response.model_dump())
    except Exception:
        pass

    return {
        key: masked for key, value in attributes.items()
        if (masked := config.mask(key, value)) is not None
    }


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

                # Buffered raw responses cache their public parse result. Use
                # that model for telemetry but return the original raw object.
                parsed = response
                if callable(getattr(type(response), "parse", None)):
                    if (not response.http_response.is_stream_consumed
                            or inspect.iscoroutinefunction(response.parse)):
                        return _RawNonStreamResponse(
                            response, span, asynchronous=False, config=self._config,
                            get_attributes=_get_output_attributes,
                        )
                    try:
                        parsed = response.parse()
                    except Exception:
                        # A caller's later parse must retain native error timing.
                        parsed = None
                output_attrs = _get_output_attributes(parsed, config=self._config)
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
            return _RawStreamResponse(stream, span, asynchronous=False, config=self._config)
        return _Stream(stream, span, config=self._config)


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

                parsed = response
                if callable(getattr(type(response), "parse", None)):
                    if not response.http_response.is_stream_consumed:
                        return _RawNonStreamResponse(
                            response, span, asynchronous=True, config=self._config,
                            get_attributes=_get_output_attributes,
                        )
                    try:
                        parsed = response.parse()
                        if inspect.isawaitable(parsed):
                            parsed = await parsed
                    except Exception:
                        parsed = None
                output_attrs = _get_output_attributes(parsed, config=self._config)
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
            return _RawStreamResponse(stream, span, asynchronous=True, config=self._config)
        return _AsyncStream(stream, span, config=self._config)
