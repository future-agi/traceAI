import logging
from abc import ABC
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, Mapping, Tuple

import opentelemetry.context as context_api
from fi_instrumentation import get_attributes_from_context
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN
from opentelemetry.util.types import AttributeValue

from traceai_huggingface._request_attributes_extractor import (
    _ChatCompletionRequestAttributesExtractor,
    _FeatureExtractionRequestAttributesExtractor,
    _TextGenerationRequestAttributesExtractor,
)
from traceai_huggingface._response_attributes_extractor import (
    _ChatCompletionResponseAttributesExtractor,
    _FeatureExtractionResponseAttributesExtractor,
    _TextGenerationResponseAttributesExtractor,
)
from traceai_huggingface._stream import _AsyncStream, _Stream, _StreamState
from traceai_huggingface._utils import _finish_tracing
from traceai_huggingface._with_span import _WithSpan

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def _private_request_attributes(attributes, config):
    """Avoid raw input copies outside FITracer's semantic privacy keys."""
    for key, value in attributes:
        if (config.hide_inputs or config.hide_input_text) and key in (
            "huggingface.tools", "huggingface.tool_choice"
        ):
            continue
        if config.hide_input_text and key == SpanAttributes.INPUT_VALUE:
            continue
        yield key, value


def _private_response_attributes(attributes, config, *, chat=False, request=None):
    """FITracer masks messages, but cannot mask the raw output duplicate."""
    hide_raw = config.hide_output_messages or config.hide_output_text
    hide_full_text = (
        not chat
        and (config.hide_inputs or config.hide_input_text)
        and request is not None
        and request.get("return_full_text") is True
    )
    for key, value in attributes:
        if key == SpanAttributes.OUTPUT_VALUE and (hide_raw or hide_full_text):
            continue
        if hide_full_text and key.startswith(SpanAttributes.GEN_AI_OUTPUT_MESSAGES):
            continue
        yield key, value


class _WithTracer(ABC):
    """Base class for wrappers that need a tracer."""

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


class _TextGenerationWrapper(_WithTracer):
    """Wrapper for HuggingFace text_generation."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _TextGenerationRequestAttributesExtractor()
        self._response_extractor = _TextGenerationResponseAttributesExtractor()

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        # Extract prompt from args
        request_parameters = dict(kwargs)
        if args:
            request_parameters["prompt"] = args[0]

        # Get model from instance if not in kwargs
        if "model" not in request_parameters and hasattr(instance, "model"):
            request_parameters["model"] = instance.model

        span_name = "huggingface.text_generation"

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=_private_request_attributes(
                self._request_extractor.get_extra_attributes_from_request(request_parameters),
                self._tracer._self_config,
            ),
        ) as span:
            try:
                response = wrapped(*args, **kwargs)
            except BaseException as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            # Check if streaming
            if kwargs.get("stream", False):
                return self._handle_streaming_response(span, response, request_parameters)

            try:
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._response_extractor.get_attributes(response),
                    extra_attributes=_private_response_attributes(
                        self._response_extractor.get_extra_attributes(response, request_parameters),
                        self._tracer._self_config,
                        chat=False,
                        request=request_parameters,
                    ),
                )
            except Exception:
                logger.exception("Failed to finalize response")
                span.finish_tracing()
            return response

    def _handle_streaming_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ):
        return _Stream(
            response,
            _StreamState(
                span, self._response_extractor, request_parameters,
                chat=False, config=self._tracer._self_config,
            ),
        )


class _AsyncTextGenerationWrapper(_WithTracer):
    """Async wrapper for HuggingFace text_generation."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _TextGenerationRequestAttributesExtractor()
        self._response_extractor = _TextGenerationResponseAttributesExtractor()

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)

        request_parameters = dict(kwargs)
        if args:
            request_parameters["prompt"] = args[0]

        if "model" not in request_parameters and hasattr(instance, "model"):
            request_parameters["model"] = instance.model

        span_name = "huggingface.text_generation"

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=_private_request_attributes(
                self._request_extractor.get_extra_attributes_from_request(request_parameters),
                self._tracer._self_config,
            ),
        ) as span:
            try:
                response = await wrapped(*args, **kwargs)
            except BaseException as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            if kwargs.get("stream", False):
                return _AsyncStream(
                    response,
                    _StreamState(
                span, self._response_extractor, request_parameters,
                chat=False, config=self._tracer._self_config,
            ),
                )

            try:
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._response_extractor.get_attributes(response),
                    extra_attributes=_private_response_attributes(
                        self._response_extractor.get_extra_attributes(response, request_parameters),
                        self._tracer._self_config,
                        chat=False,
                        request=request_parameters,
                    ),
                )
            except Exception:
                logger.exception("Failed to finalize response")
                span.finish_tracing()
            return response


class _ChatCompletionWrapper(_WithTracer):
    """Wrapper for HuggingFace chat_completion."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _ChatCompletionRequestAttributesExtractor()
        self._response_extractor = _ChatCompletionResponseAttributesExtractor()

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        request_parameters = dict(kwargs)
        if args:
            request_parameters["messages"] = args[0]

        if "model" not in request_parameters and hasattr(instance, "model"):
            request_parameters["model"] = instance.model

        span_name = "huggingface.chat_completion"

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=_private_request_attributes(
                self._request_extractor.get_extra_attributes_from_request(request_parameters),
                self._tracer._self_config,
            ),
        ) as span:
            try:
                response = wrapped(*args, **kwargs)
            except BaseException as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            # Check if streaming
            if kwargs.get("stream", False):
                return self._handle_streaming_response(span, response, request_parameters)

            try:
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._response_extractor.get_attributes(response),
                    extra_attributes=_private_response_attributes(
                        self._response_extractor.get_extra_attributes(response, request_parameters),
                        self._tracer._self_config,
                        chat=True,
                        request=request_parameters,
                    ),
                )
            except Exception:
                logger.exception("Failed to finalize response")
                span.finish_tracing()
            return response

    def _handle_streaming_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ):
        return _Stream(
            response,
            _StreamState(
                span, self._response_extractor, request_parameters,
                chat=True, config=self._tracer._self_config,
            ),
        )


class _AsyncChatCompletionWrapper(_WithTracer):
    """Async wrapper for HuggingFace chat_completion."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _ChatCompletionRequestAttributesExtractor()
        self._response_extractor = _ChatCompletionResponseAttributesExtractor()

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)

        request_parameters = dict(kwargs)
        if args:
            request_parameters["messages"] = args[0]

        if "model" not in request_parameters and hasattr(instance, "model"):
            request_parameters["model"] = instance.model

        span_name = "huggingface.chat_completion"

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=_private_request_attributes(
                self._request_extractor.get_extra_attributes_from_request(request_parameters),
                self._tracer._self_config,
            ),
        ) as span:
            try:
                response = await wrapped(*args, **kwargs)
            except BaseException as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            if kwargs.get("stream", False):
                return _AsyncStream(
                    response,
                    _StreamState(
                span, self._response_extractor, request_parameters,
                chat=True, config=self._tracer._self_config,
            ),
                )

            try:
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._response_extractor.get_attributes(response),
                    extra_attributes=_private_response_attributes(
                        self._response_extractor.get_extra_attributes(response, request_parameters),
                        self._tracer._self_config,
                        chat=True,
                        request=request_parameters,
                    ),
                )
            except Exception:
                logger.exception("Failed to finalize response")
                span.finish_tracing()
            return response


class _FeatureExtractionWrapper(_WithTracer):
    """Wrapper for HuggingFace feature_extraction (embeddings)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _FeatureExtractionRequestAttributesExtractor()
        self._response_extractor = _FeatureExtractionResponseAttributesExtractor()

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        request_parameters = dict(kwargs)
        if args:
            request_parameters["text"] = args[0]

        if "model" not in request_parameters and hasattr(instance, "model"):
            request_parameters["model"] = instance.model

        span_name = "huggingface.feature_extraction"

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
        ) as span:
            try:
                response = wrapped(*args, **kwargs)
            except BaseException as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            try:
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._response_extractor.get_attributes(response),
                    extra_attributes=self._response_extractor.get_extra_attributes(
                        response, request_parameters
                    ),
                )
            except Exception:
                logger.exception("Failed to finalize response")
                span.finish_tracing()
            return response


class _AsyncFeatureExtractionWrapper(_WithTracer):
    """Async wrapper for HuggingFace feature_extraction."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _FeatureExtractionRequestAttributesExtractor()
        self._response_extractor = _FeatureExtractionResponseAttributesExtractor()

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)

        request_parameters = dict(kwargs)
        if args:
            request_parameters["text"] = args[0]

        if "model" not in request_parameters and hasattr(instance, "model"):
            request_parameters["model"] = instance.model

        span_name = "huggingface.feature_extraction"

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
        ) as span:
            try:
                response = await wrapped(*args, **kwargs)
            except BaseException as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            try:
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._response_extractor.get_attributes(response),
                    extra_attributes=self._response_extractor.get_extra_attributes(
                        response, request_parameters
                    ),
                )
            except Exception:
                logger.exception("Failed to finalize response")
                span.finish_tracing()
            return response
