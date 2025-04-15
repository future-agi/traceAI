import json
import logging
from abc import ABC
from contextlib import contextmanager
from enum import Enum
from inspect import Signature, signature
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Tuple

import opentelemetry.context as context_api
from fi_instrumentation import get_attributes_from_context, safe_json_dumps
from fi_instrumentation.fi_types import (
    EmbeddingAttributes,
    FiSpanKindValues,
    MessageAttributes,
    SpanAttributes,
)
from groq import NOT_GIVEN
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN
from opentelemetry.util.types import AttributeValue
from traceai_groq._request_attributes_extractor import _RequestAttributesExtractor
from traceai_groq._response_attributes_extractor import _ResponseAttributesExtractor
from traceai_groq._utils import _finish_tracing, _to_dict
from traceai_groq._with_span import _WithSpan

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def _flatten(mapping: Mapping[str, Any]) -> Iterator[Tuple[str, AttributeValue]]:
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, Mapping):
            for sub_key, sub_value in _flatten(value):
                yield f"{key}.{sub_key}", sub_value
        elif isinstance(value, List) and any(
            isinstance(item, Mapping) for item in value
        ):
            for index, sub_mapping in enumerate(value):
                for sub_key, sub_value in _flatten(sub_mapping):
                    yield f"{key}.{index}.{sub_key}", sub_value
        else:
            if isinstance(value, Enum):
                value = value.value
            yield key, value


class _WithTracer(ABC):
    """
    Base class for wrappers that need a tracer.
    """

    def __init__(self, tracer: trace_api.Tracer, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tracer = tracer

    @contextmanager
    def _start_as_current_span(
        self,
        span_name: str,
        attributes: Iterable[Tuple[str, AttributeValue]],
        context_attributes: Iterable[Tuple[str, AttributeValue]],
        extra_attributes: Iterable[Tuple[str, AttributeValue]],
    ) -> Iterator[_WithSpan]:
        # Because OTEL has a default limit of 128 attributes, we split our
        # attributes into two tiers, where "extra_attributes" are added first to
        # ensure that the most important "attributes" are added last and are not
        # dropped.
        try:
            span = self._tracer.start_span(
                name=span_name, attributes=dict(extra_attributes)
            )
        except Exception:
            span = INVALID_SPAN
        with trace_api.use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            yield _WithSpan(
                span=span,
                context_attributes=dict(context_attributes),
                extra_attributes=dict(attributes),
            )


def _parse_args(
    signature: Signature,
    *args: Tuple[Any],
    **kwargs: Mapping[str, Any],
) -> Dict[str, Any]:
    bound_signature = signature.bind(*args, **kwargs)
    bound_signature.apply_defaults()
    bound_arguments = bound_signature.arguments  # Defaults empty to NOT_GIVEN
    request_data: Dict[str, Any] = {}
    for key, value in bound_arguments.items():
        try:
            if value is not None and value is not NOT_GIVEN:
                try:
                    # ensure the value is JSON-serializable
                    safe_json_dumps(value)
                    request_data[key] = value
                except json.JSONDecodeError:
                    request_data[key] = str(value)
        except Exception:
            request_data[key] = str(value)
    return request_data


class _CompletionsWrapper(_WithTracer):
    """
    Wrapper for the pipeline processing
    Captures all calls to the pipeline
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor()
        self._content: List[str] = []
        self._raw_data: List[Dict[str, Any]] = []

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        # Prepare invocation parameters by merging args and kwargs
        invocation_parameters = {}
        for arg in args:
            if arg and isinstance(arg, dict):
                invocation_parameters.update(arg)
        invocation_parameters.update(kwargs)
        request_parameters = _parse_args(signature(wrapped), *args, **kwargs)
        span_name = "Completions"

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(
                request_parameters
            ),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(
                request_parameters
            ),
        ) as span:
            try:
                response = wrapped(*args, **kwargs)
                streaming = request_parameters.get("stream", False)
            except Exception as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            if streaming and isinstance(response, Iterable):
                # If streaming, wrap the iterator without consuming it
                def streaming_response_wrapper():
                    try:
                        for item in response:
                            raw_chunk = _to_dict(item)
                            self._raw_data.append(raw_chunk)

                            if isinstance(raw_chunk, dict):
                                choices = raw_chunk.get("choices", [])
                                if choices and isinstance(choices[0], dict):
                                    delta = choices[0].get("delta", {})
                                    if content := delta.get("content"):
                                        self._content.append(content)
                            yield item

                    except Exception as exception:
                        span.record_exception(exception)
                        status = trace_api.Status(
                            status_code=trace_api.StatusCode.ERROR,
                            description=f"{type(exception).__name__}: {exception}",
                        )
                        span.finish_tracing(status=status)
                        raise
                    else:
                        try:
                            output_value = "".join(self._content)

                            # Set the RAW_OUTPUT span attribute
                            span._span.set_attribute(
                                SpanAttributes.RAW_OUTPUT,
                                safe_json_dumps(self._raw_data),
                            )
                            span._span.set_attribute(
                                SpanAttributes.OUTPUT_VALUE, output_value
                            )

                            _finish_tracing(
                                status=trace_api.Status(
                                    status_code=trace_api.StatusCode.OK
                                ),
                                with_span=span,
                                attributes=self._response_extractor.get_attributes(
                                    response=None, is_streaming=streaming
                                ),
                                extra_attributes=self._response_extractor.get_extra_attributes(
                                    response=None,
                                    request_parameters=request_parameters,
                                    is_streaming=streaming,
                                ),
                            )
                        except Exception:
                            logger.exception(
                                f"Failed to finalize streaming response of type {type(response)}"
                            )
                            span.finish_tracing()
                    finally:
                        if span._span.is_recording():
                            span.finish_tracing()

                return streaming_response_wrapper()

                return streaming_response_wrapper()
            else:
                # Non-streaming response
                try:
                    _finish_tracing(
                        status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                        with_span=span,
                        attributes=self._response_extractor.get_attributes(
                            response=response, is_streaming=streaming
                        ),
                        extra_attributes=self._response_extractor.get_extra_attributes(
                            response=response,
                            request_parameters=request_parameters,
                            is_streaming=streaming,
                        ),
                    )
                except Exception:
                    logger.exception(
                        f"Failed to finalize response of type {type(response)}"
                    )
                    span.finish_tracing()
                return response


class _AsyncCompletionsWrapper(_WithTracer):
    """
    Wrapper for the pipeline processing
    Captures all asynchronous calls to the pipeline
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor()
        self._content: List[str] = []
        self._raw_data: List[Dict[str, Any]] = []

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)

        # Prepare invocation parameters by merging args and kwargs
        invocation_parameters = {}
        for arg in args:
            if arg and isinstance(arg, dict):
                invocation_parameters.update(arg)
        invocation_parameters.update(kwargs)
        request_parameters = _parse_args(signature(wrapped), *args, **kwargs)

        span_name = "AsyncCompletions"
        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(
                request_parameters
            ),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(
                request_parameters
            ),
        ) as span:
            try:
                response = await wrapped(*args, **kwargs)
                streaming = request_parameters.get("stream", False)
            except Exception as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            if streaming and hasattr(response, "__aiter__"):
                # If streaming, wrap the async iterator without consuming it
                async def streaming_response_wrapper():
                    try:
                        async for item in response:
                            raw_chunk = _to_dict(item)
                            self._raw_data.append(raw_chunk)

                            if isinstance(raw_chunk, dict):
                                choices = raw_chunk.get("choices", [])
                                if choices and isinstance(choices[0], dict):
                                    delta = choices[0].get("delta", {})
                                    if content := delta.get("content"):
                                        self._content.append(content)
                            yield item

                    except Exception as exception:
                        span.record_exception(exception)
                        status = trace_api.Status(
                            status_code=trace_api.StatusCode.ERROR,
                            description=f"{type(exception).__name__}: {exception}",
                        )
                        span.finish_tracing(status=status)
                        raise
                    else:
                        try:
                            output_value = "".join(self._content)

                            # Set the RAW_OUTPUT span attribute
                            span._span.set_attribute(
                                SpanAttributes.RAW_OUTPUT,
                                safe_json_dumps(self._raw_data),
                            )
                            span._span.set_attribute(
                                SpanAttributes.OUTPUT_VALUE, output_value
                            )

                            _finish_tracing(
                                status=trace_api.Status(
                                    status_code=trace_api.StatusCode.OK
                                ),
                                with_span=span,
                                attributes=self._response_extractor.get_attributes(
                                    response=None, is_streaming=streaming
                                ),
                                extra_attributes=self._response_extractor.get_extra_attributes(
                                    response=None,
                                    request_parameters=request_parameters,
                                    is_streaming=streaming,
                                ),
                            )
                        except Exception:
                            logger.exception(
                                f"Failed to finalize streaming response of type {type(response)}"
                            )
                            span.finish_tracing()
                    finally:
                        if span._span.is_recording():
                            span.finish_tracing()

                return streaming_response_wrapper()
            else:
                # Non-streaming response
                try:
                    _finish_tracing(
                        status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                        with_span=span,
                        attributes=self._response_extractor.get_attributes(
                            response=response, is_streaming=streaming
                        ),
                        extra_attributes=self._response_extractor.get_extra_attributes(
                            response=response,
                            request_parameters=request_parameters,
                            is_streaming=streaming,
                        ),
                    )
                except Exception:
                    logger.exception(
                        f"Failed to finalize response of type {type(response)}"
                    )
                    span.finish_tracing()
        return response


CHAIN = FiSpanKindValues.CHAIN
RETRIEVER = FiSpanKindValues.RETRIEVER
EMBEDDING = FiSpanKindValues.EMBEDDING
LLM = FiSpanKindValues.LLM
LLM_OUTPUT_MESSAGES = SpanAttributes.LLM_OUTPUT_MESSAGES
MESSAGE_CONTENT = MessageAttributes.MESSAGE_CONTENT
MESSAGE_ROLE = MessageAttributes.MESSAGE_ROLE
EMBEDDING_VECTOR = EmbeddingAttributes.EMBEDDING_VECTOR
EMBEDDING_TEXT = EmbeddingAttributes.EMBEDDING_TEXT
LLM_TOKEN_COUNT_COMPLETION = SpanAttributes.LLM_TOKEN_COUNT_COMPLETION
