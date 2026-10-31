import logging
from abc import ABC
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Iterator, Mapping, Tuple

import opentelemetry.context as context_api
from fi_instrumentation import TraceConfig, get_attributes_from_context
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN
from opentelemetry.util.types import AttributeValue

from traceai_cohere._request_attributes_extractor import (
    _ChatRequestAttributesExtractor,
    _EmbedRequestAttributesExtractor,
    _RerankRequestAttributesExtractor,
)
from traceai_cohere._response_attributes_extractor import (
    _ChatResponseAttributesExtractor,
    _EmbedResponseAttributesExtractor,
    _RerankResponseAttributesExtractor,
)
from traceai_cohere._stream import _AsyncStream, _Stream
from traceai_cohere._utils import _finish_tracing, _to_dict
from traceai_cohere._with_span import _WithSpan

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _WithTracer(ABC):
    """Base class for wrappers that need a tracer."""

    def __init__(self, tracer: trace_api.Tracer, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tracer = tracer
        # Use the exact FITracer config: explicit options override environment values.
        config = getattr(tracer, "_self_config", None)
        self._config = config if isinstance(config, TraceConfig) else TraceConfig()

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


class _ChatWrapper(_WithTracer):
    """Wrapper for Cohere chat."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _ChatRequestAttributesExtractor()
        self._response_extractor = _ChatResponseAttributesExtractor()

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

        span_name = "cohere.chat"

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
            except Exception as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            try:
                response_dict = _to_dict(response)
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._response_extractor.get_attributes(response_dict),
                    extra_attributes=self._response_extractor.get_extra_attributes(
                        response_dict, request_parameters
                    ),
                )
            except Exception:
                logger.exception("Failed to finalize response")
                span.finish_tracing()
            return response


class _AsyncChatWrapper(_WithTracer):
    """Async wrapper for Cohere chat."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _ChatRequestAttributesExtractor()
        self._response_extractor = _ChatResponseAttributesExtractor()

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
        span_name = "cohere.chat"

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
            except Exception as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            try:
                response_dict = _to_dict(response)
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._response_extractor.get_attributes(response_dict),
                    extra_attributes=self._response_extractor.get_extra_attributes(
                        response_dict, request_parameters
                    ),
                )
            except Exception:
                logger.exception("Failed to finalize response")
                span.finish_tracing()
            return response


class _ChatStreamWrapper(_WithTracer):
    """Wrapper for Cohere chat_stream."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _ChatRequestAttributesExtractor()
        self._response_extractor = _ChatResponseAttributesExtractor()

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
        span_name = "cohere.chat_stream"

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
            except Exception as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            return self._handle_streaming_response(span, response, request_parameters)

    def _handle_streaming_response(self, span, response, request_parameters):
        return _Stream(response, span, request_parameters)


class _AsyncChatStreamWrapper(_ChatStreamWrapper):
    """Async SDK streams return an async iterator directly, not an awaitable."""

    def _handle_streaming_response(self, span, response, request_parameters):
        return _AsyncStream(response, span, request_parameters)


class _EmbedWrapper(_WithTracer):
    """Wrapper for Cohere embed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _EmbedRequestAttributesExtractor()
        self._response_extractor = _EmbedResponseAttributesExtractor()

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
        span_name = "cohere.embed"

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
            except Exception as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            try:
                response_dict = _to_dict(response)
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._response_extractor.get_attributes(response_dict),
                    extra_attributes=self._response_extractor.get_extra_attributes(
                        response_dict, request_parameters
                    ),
                )
            except Exception:
                logger.exception("Failed to finalize response")
                span.finish_tracing()
            return response


class _AsyncEmbedWrapper(_WithTracer):
    """Async wrapper for Cohere embed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _EmbedRequestAttributesExtractor()
        self._response_extractor = _EmbedResponseAttributesExtractor()

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
        span_name = "cohere.embed"

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
            except Exception as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            try:
                response_dict = _to_dict(response)
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._response_extractor.get_attributes(response_dict),
                    extra_attributes=self._response_extractor.get_extra_attributes(
                        response_dict, request_parameters
                    ),
                )
            except Exception:
                logger.exception("Failed to finalize response")
                span.finish_tracing()
            return response


class _RerankWrapper(_WithTracer):
    """Wrapper for Cohere rerank."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RerankRequestAttributesExtractor(config=self._config)
        self._response_extractor = _RerankResponseAttributesExtractor()

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
        span_name = "cohere.rerank"

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
            except Exception as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            try:
                response_dict = _to_dict(response)
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._response_extractor.get_attributes(response_dict),
                    extra_attributes=self._response_extractor.get_extra_attributes(
                        response_dict, request_parameters
                    ),
                )
            except Exception:
                logger.exception("Failed to finalize response")
                span.finish_tracing()
            return response


class _AsyncRerankWrapper(_WithTracer):
    """Async wrapper for Cohere rerank."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RerankRequestAttributesExtractor(config=self._config)
        self._response_extractor = _RerankResponseAttributesExtractor()

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
        span_name = "cohere.rerank"

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
            except Exception as exception:
                span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(exception).__name__}: {exception}",
                )
                span.finish_tracing(status=status)
                raise

            try:
                response_dict = _to_dict(response)
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._response_extractor.get_attributes(response_dict),
                    extra_attributes=self._response_extractor.get_extra_attributes(
                        response_dict, request_parameters
                    ),
                )
            except Exception:
                logger.exception("Failed to finalize response")
                span.finish_tracing()
            return response
