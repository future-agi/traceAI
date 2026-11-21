import logging
from abc import ABC
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

import opentelemetry.context as context_api
from fi_instrumentation import get_attributes_from_context, safe_json_dumps
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN
from opentelemetry.util.types import AttributeValue

from traceai_together._request_attributes_extractor import (
    _ChatCompletionsRequestAttributesExtractor,
    _CompletionsRequestAttributesExtractor,
    _EmbeddingsRequestAttributesExtractor,
)
from traceai_together._response_attributes_extractor import (
    _ChatCompletionsResponseAttributesExtractor,
    _CompletionsResponseAttributesExtractor,
    _EmbeddingsResponseAttributesExtractor,
    _StreamingChatResponseAccumulator,
    _StreamingCompletionsResponseAccumulator,
)
from traceai_together._stream import TracedAsyncStream, TracedStream
from traceai_together._utils import _finish_tracing, _response_dict
from traceai_together._with_span import _WithSpan

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _WithTracer(ABC):
    """Base class for wrappers that need a tracer."""

    def __init__(self, tracer: trace_api.Tracer, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tracer = tracer

    def _stream_callbacks(self, span: _WithSpan, accumulator: Any, request_parameters: Dict[str, Any], content: str):
        """The per-chunk and end-of-stream callbacks of a traced stream (``content`` names the accumulator getter)."""
        state = {"first": True}

        def on_chunk(chunk: Any) -> None:
            if state["first"]:
                span.add_event("First Token Stream Event")
                state["first"] = False
            accumulator.process_chunk(chunk)

        def finish(error: Optional[BaseException], incomplete: bool) -> None:
            if error is not None:
                span.record_exception(error)
                span.finish_tracing(status=trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(error).__name__}: {error}",
                ))
                return
            try:
                final_response = accumulator.get_accumulated_response()
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, getattr(accumulator, content)())
                if incomplete:
                    span.set_attribute("together.stream.incomplete", True)
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._response_extractor.get_attributes(final_response, is_streaming=True),
                    extra_attributes=self._response_extractor.get_extra_attributes(
                        final_response, request_parameters, is_streaming=True
                    ),
                )
            except Exception:
                logger.exception("Failed to finalize streaming response")
                span.finish_tracing()

        return on_chunk, finish

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
            logger.exception("Failed to start span")
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


# ============================================================================
# Chat Completions Wrappers
# ============================================================================


class _ChatCompletionsWrapper(_WithTracer):
    """Wrapper for Together AI chat.completions.create (sync)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _ChatCompletionsRequestAttributesExtractor()
        self._response_extractor = _ChatCompletionsResponseAttributesExtractor()

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
        span_name = "together.chat.completions"
        is_streaming = request_parameters.get("stream", False)

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
        ) as span:
            span.set_attribute(SpanAttributes.INPUT_VALUE, safe_json_dumps(request_parameters))

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

            if is_streaming:
                return self._handle_streaming_response(span, response, request_parameters)
            else:
                try:
                    response_dict = _response_dict(response, self._tracer)
                    span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response_dict))
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

    def _handle_streaming_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ):
        """Wrap the SDK's stream so its span ends once, whichever way the stream ends."""
        on_chunk, finish = self._stream_callbacks(span, _StreamingChatResponseAccumulator(), request_parameters, "get_content")
        return TracedStream(response, on_chunk, finish)


class _AsyncChatCompletionsWrapper(_WithTracer):
    """Async wrapper for Together AI chat.completions.create."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _ChatCompletionsRequestAttributesExtractor()
        self._response_extractor = _ChatCompletionsResponseAttributesExtractor()

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
        span_name = "together.chat.completions"
        is_streaming = request_parameters.get("stream", False)

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
        ) as span:
            span.set_attribute(SpanAttributes.INPUT_VALUE, safe_json_dumps(request_parameters))

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

            if is_streaming:
                return self._handle_async_streaming_response(span, response, request_parameters)
            else:
                try:
                    response_dict = _response_dict(response, self._tracer)
                    span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response_dict))
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

    def _handle_async_streaming_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ):
        """Wrap the SDK's async stream so its span ends once, whichever way the stream ends."""
        on_chunk, finish = self._stream_callbacks(span, _StreamingChatResponseAccumulator(), request_parameters, "get_content")
        return TracedAsyncStream(response, on_chunk, finish)


# ============================================================================
# Completions Wrappers
# ============================================================================


class _CompletionsWrapper(_WithTracer):
    """Wrapper for Together AI completions.create (sync)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _CompletionsRequestAttributesExtractor()
        self._response_extractor = _CompletionsResponseAttributesExtractor()

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
        span_name = "together.completions"
        is_streaming = request_parameters.get("stream", False)

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
        ) as span:
            span.set_attribute(SpanAttributes.INPUT_VALUE, safe_json_dumps(request_parameters))

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

            if is_streaming:
                return self._handle_streaming_response(span, response, request_parameters)
            else:
                try:
                    response_dict = _response_dict(response, self._tracer)
                    span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response_dict))
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

    def _handle_streaming_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ):
        """Wrap the SDK's stream so its span ends once, whichever way the stream ends."""
        on_chunk, finish = self._stream_callbacks(span, _StreamingCompletionsResponseAccumulator(), request_parameters, "get_text")
        return TracedStream(response, on_chunk, finish)


class _AsyncCompletionsWrapper(_WithTracer):
    """Async wrapper for Together AI completions.create."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _CompletionsRequestAttributesExtractor()
        self._response_extractor = _CompletionsResponseAttributesExtractor()

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
        span_name = "together.completions"
        is_streaming = request_parameters.get("stream", False)

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
        ) as span:
            span.set_attribute(SpanAttributes.INPUT_VALUE, safe_json_dumps(request_parameters))

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

            if is_streaming:
                return self._handle_async_streaming_response(span, response, request_parameters)
            else:
                try:
                    response_dict = _response_dict(response, self._tracer)
                    span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response_dict))
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

    def _handle_async_streaming_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ):
        """Wrap the SDK's async stream so its span ends once, whichever way the stream ends."""
        on_chunk, finish = self._stream_callbacks(span, _StreamingCompletionsResponseAccumulator(), request_parameters, "get_text")
        return TracedAsyncStream(response, on_chunk, finish)


# ============================================================================
# Embeddings Wrappers
# ============================================================================


class _EmbeddingsWrapper(_WithTracer):
    """Wrapper for Together AI embeddings.create (sync)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _EmbeddingsRequestAttributesExtractor()
        self._response_extractor = _EmbeddingsResponseAttributesExtractor()

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
        span_name = "together.embeddings"

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
        ) as span:
            span.set_attribute(SpanAttributes.INPUT_VALUE, safe_json_dumps(request_parameters))

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
                response_dict = _response_dict(response, self._tracer)
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response_dict))
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


class _AsyncEmbeddingsWrapper(_WithTracer):
    """Async wrapper for Together AI embeddings.create."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _EmbeddingsRequestAttributesExtractor()
        self._response_extractor = _EmbeddingsResponseAttributesExtractor()

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
        span_name = "together.embeddings"

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
        ) as span:
            span.set_attribute(SpanAttributes.INPUT_VALUE, safe_json_dumps(request_parameters))

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
                response_dict = _response_dict(response, self._tracer)
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response_dict))
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
