from __future__ import annotations

import json
from typing import Any, Callable

import wrapt
from opentelemetry import context as context_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import Status, StatusCode, Tracer

from traceai_bedrock._attribute_extractor import AttributeExtractor
from traceai_bedrock._rag_wrappers import _RagEventStream
from traceai_bedrock._response_accumulator import _ResponseAccumulator
from traceai_bedrock.utils import _EventStream, _use_span
from traceai_bedrock.utils._invoke_model_stream import (
    _InvokeModelEventStream,
    _InvokeModelStreamCallback,
)
from traceai_bedrock.utils.anthropic._messages import (
    _AnthropicMessagesCallback,
)
from fi_instrumentation.fi_types import (
    ImageAttributes,
    MessageAttributes,
    MessageContentAttributes,
    FiLLMProviderValues,
    FiMimeTypeValues,
    FiSpanKindValues,
    SpanAttributes,
)


class _WithTracer:
    def __init__(self, tracer: Tracer):
        self._tracer = tracer


class _InvokeModelWithResponseStream(_WithTracer):
    _name = "bedrock.invoke_model_with_response_stream"

    @wrapt.decorator  # type: ignore[misc]
    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        with self._tracer.start_as_current_span(
            self._name,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            from botocore.eventstream import EventStream

            # Do not mutate the caller's kwargs or read a file-like request body.
            # Parsing for telemetry must not prevent a valid provider invocation.
            body = None
            if isinstance(raw_body := kwargs.get("body"), (str, bytes, bytearray)):
                try:
                    body = json.loads(raw_body)
                except (TypeError, ValueError):
                    pass
            request = {**kwargs, "body": body}
            callback: Callable[[Any], Any]
            stream_type: type[_EventStream]
            if isinstance(body, dict) and "anthropic_version" in body:
                callback = _AnthropicMessagesCallback(span, request)
                stream_type = _EventStream
            else:
                callback = _InvokeModelStreamCallback(span, request)
                stream_type = _InvokeModelEventStream
            try:
                response = wrapped(*args, **kwargs)
            except BaseException as exc:
                callback(exc)
                raise
            if isinstance(response["body"], EventStream):
                response["body"] = stream_type(response["body"], callback, _use_span(span))
            else:
                callback(StopIteration())
            return response


class _InvokeAgentWithResponseStream(_WithTracer):
    _name = "bedrock_agent.invoke_agent"

    @wrapt.decorator  # type: ignore[misc]
    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        # span = self._tracer.start_span(self._name)
        with self._tracer.start_as_current_span(
            self._name,
            end_on_exit=False,
        ) as span:
            attributes = {
                SpanAttributes.GEN_AI_SPAN_KIND: FiSpanKindValues.AGENT.value,
                SpanAttributes.GEN_AI_PROVIDER_NAME: FiLLMProviderValues.AWS.value,
            }
            if input_text := kwargs.get("inputText"):
                attributes[SpanAttributes.INPUT_VALUE] = input_text
            span.set_attributes({k: v for k, v in attributes.items() if v is not None})
            try:
                response = wrapped(*args, **kwargs)
                response["completion"] = _EventStream(
                    response["completion"],
                    _ResponseAccumulator(span, self._tracer, kwargs),
                    _use_span(span),
                )
                return response
            except Exception as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.end()
                raise e


class _RetrieveAndGenerateStream(_WithTracer):
    _name = "bedrock_agent.retrieve_and_generate_stream"

    @wrapt.decorator  # type: ignore[misc]
    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        with self._tracer.start_as_current_span(
            self._name,
            end_on_exit=False,
        ) as span:
            try:
                span.set_attributes(AttributeExtractor.extract_bedrock_rag_input_attributes(kwargs))
                response = wrapped(*args, **kwargs)
                response["stream"] = _EventStream(
                    response["stream"],
                    _RagEventStream(span, self._tracer, kwargs),
                    _use_span(span),
                )
                return response
            except Exception as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.end()
                raise e


INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
INPUT_VALUE = SpanAttributes.INPUT_VALUE
JSON = FiMimeTypeValues.JSON.value
LLM = FiSpanKindValues.LLM.value
GEN_AI_INPUT_MESSAGES = SpanAttributes.GEN_AI_INPUT_MESSAGES
GEN_AI_REQUEST_PARAMETERS = SpanAttributes.GEN_AI_REQUEST_PARAMETERS
GEN_AI_REQUEST_MODEL = SpanAttributes.GEN_AI_REQUEST_MODEL
GEN_AI_USAGE_OUTPUT_TOKENS = SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS
GEN_AI_USAGE_INPUT_TOKENS = SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS
GEN_AI_USAGE_TOTAL_TOKENS = SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS
MESSAGE_CONTENT = MessageAttributes.MESSAGE_CONTENT
MESSAGE_CONTENT_IMAGE = MessageContentAttributes.MESSAGE_CONTENT_IMAGE
MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON = MessageAttributes.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON
MESSAGE_FUNCTION_CALL_NAME = MessageAttributes.MESSAGE_FUNCTION_CALL_NAME
MESSAGE_NAME = MessageAttributes.MESSAGE_NAME
GEN_AI_SPAN_KIND = SpanAttributes.GEN_AI_SPAN_KIND
OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
