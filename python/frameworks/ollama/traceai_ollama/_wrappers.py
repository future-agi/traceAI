import json
import logging
from abc import ABC
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Tuple

import opentelemetry.context as context_api
from fi_instrumentation import get_attributes_from_context, safe_json_dumps
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN
from opentelemetry.util.types import AttributeValue

from traceai_ollama._request_attributes_extractor import _RequestAttributesExtractor
from traceai_ollama._response_attributes_extractor import _ResponseAttributesExtractor
from traceai_ollama._utils import _finish_tracing, _to_dict
from traceai_ollama._with_span import _WithSpan

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


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


class _ChatWrapper(_WithTracer):
    """Wrapper for Ollama chat completions."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor()

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        # Extract request parameters
        request_parameters = dict(kwargs)
        if args:
            request_parameters["model"] = args[0] if len(args) > 0 else kwargs.get("model")
            if len(args) > 1:
                request_parameters["messages"] = args[1]

        span_name = f"ollama.chat"
        streaming = request_parameters.get("stream", False)

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
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

            if streaming and isinstance(response, Iterable):
                return self._handle_streaming_response(span, response, request_parameters)
            else:
                return self._handle_response(span, response, request_parameters)

    def _handle_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ) -> Any:
        """Handle non-streaming response."""
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

    def _handle_streaming_response(
        self, span: _WithSpan, response: Iterable, request_parameters: Dict[str, Any]
    ) -> Iterable:
        """Handle streaming response."""
        content_parts: List[str] = []
        raw_chunks: List[Dict[str, Any]] = []
        final_response: Dict[str, Any] = {}

        def streaming_wrapper():
            nonlocal final_response
            try:
                for chunk in response:
                    chunk_dict = _to_dict(chunk)
                    raw_chunks.append(chunk_dict)

                    # Accumulate content
                    if "message" in chunk_dict:
                        content = chunk_dict["message"].get("content", "")
                        if content:
                            content_parts.append(content)

                    # Capture final chunk with done=True
                    if chunk_dict.get("done", False):
                        final_response = chunk_dict

                    yield chunk

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
                    # Build final response for attribute extraction
                    final_response["message"] = {
                        "role": "assistant",
                        "content": "".join(content_parts)
                    }
                    span._span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_dumps(raw_chunks))
                    span._span.set_attribute(SpanAttributes.OUTPUT_VALUE, "".join(content_parts))

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
            finally:
                if span._span.is_recording():
                    span.finish_tracing()

        return streaming_wrapper()


class _AsyncChatWrapper(_WithTracer):
    """Async wrapper for Ollama chat completions."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor()

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
            request_parameters["model"] = args[0] if len(args) > 0 else kwargs.get("model")
            if len(args) > 1:
                request_parameters["messages"] = args[1]

        span_name = f"ollama.chat"
        streaming = request_parameters.get("stream", False)

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
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

            if streaming and hasattr(response, "__aiter__"):
                return self._handle_async_streaming_response(span, response, request_parameters)
            else:
                return self._handle_response(span, response, request_parameters)

    def _handle_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ) -> Any:
        """Handle non-streaming response."""
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

    async def _handle_async_streaming_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ):
        """Handle async streaming response."""
        content_parts: List[str] = []
        raw_chunks: List[Dict[str, Any]] = []
        final_response: Dict[str, Any] = {}

        async def streaming_wrapper():
            nonlocal final_response
            try:
                async for chunk in response:
                    chunk_dict = _to_dict(chunk)
                    raw_chunks.append(chunk_dict)

                    if "message" in chunk_dict:
                        content = chunk_dict["message"].get("content", "")
                        if content:
                            content_parts.append(content)

                    if chunk_dict.get("done", False):
                        final_response = chunk_dict

                    yield chunk

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
                    final_response["message"] = {
                        "role": "assistant",
                        "content": "".join(content_parts)
                    }
                    span._span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_dumps(raw_chunks))
                    span._span.set_attribute(SpanAttributes.OUTPUT_VALUE, "".join(content_parts))

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
            finally:
                if span._span.is_recording():
                    span.finish_tracing()

        return streaming_wrapper()


class _GenerateWrapper(_WithTracer):
    """Wrapper for Ollama generate endpoint."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor()

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
            request_parameters["model"] = args[0] if len(args) > 0 else kwargs.get("model")
            if len(args) > 1:
                request_parameters["prompt"] = args[1]

        span_name = f"ollama.generate"
        streaming = request_parameters.get("stream", False)

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
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

            if streaming and isinstance(response, Iterable):
                return self._handle_streaming_response(span, response, request_parameters)
            else:
                return self._handle_response(span, response, request_parameters)

    def _handle_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ) -> Any:
        """Handle non-streaming response."""
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

    def _handle_streaming_response(
        self, span: _WithSpan, response: Iterable, request_parameters: Dict[str, Any]
    ) -> Iterable:
        """Handle streaming response."""
        content_parts: List[str] = []
        raw_chunks: List[Dict[str, Any]] = []
        final_response: Dict[str, Any] = {}

        def streaming_wrapper():
            nonlocal final_response
            try:
                for chunk in response:
                    chunk_dict = _to_dict(chunk)
                    raw_chunks.append(chunk_dict)

                    if "response" in chunk_dict:
                        content = chunk_dict.get("response", "")
                        if content:
                            content_parts.append(content)

                    if chunk_dict.get("done", False):
                        final_response = chunk_dict

                    yield chunk

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
                    final_response["response"] = "".join(content_parts)
                    span._span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_dumps(raw_chunks))
                    span._span.set_attribute(SpanAttributes.OUTPUT_VALUE, "".join(content_parts))

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
            finally:
                if span._span.is_recording():
                    span.finish_tracing()

        return streaming_wrapper()


class _AsyncGenerateWrapper(_WithTracer):
    """Async wrapper for Ollama generate endpoint."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor()

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
            request_parameters["model"] = args[0] if len(args) > 0 else kwargs.get("model")
            if len(args) > 1:
                request_parameters["prompt"] = args[1]

        span_name = f"ollama.generate"
        streaming = request_parameters.get("stream", False)

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
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

            if streaming and hasattr(response, "__aiter__"):
                return self._handle_async_streaming_response(span, response, request_parameters)
            else:
                return self._handle_response(span, response, request_parameters)

    def _handle_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ) -> Any:
        """Handle non-streaming response."""
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

    async def _handle_async_streaming_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ):
        """Handle async streaming response."""
        content_parts: List[str] = []
        raw_chunks: List[Dict[str, Any]] = []
        final_response: Dict[str, Any] = {}

        async def streaming_wrapper():
            nonlocal final_response
            try:
                async for chunk in response:
                    chunk_dict = _to_dict(chunk)
                    raw_chunks.append(chunk_dict)

                    if "response" in chunk_dict:
                        content = chunk_dict.get("response", "")
                        if content:
                            content_parts.append(content)

                    if chunk_dict.get("done", False):
                        final_response = chunk_dict

                    yield chunk

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
                    final_response["response"] = "".join(content_parts)
                    span._span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_dumps(raw_chunks))
                    span._span.set_attribute(SpanAttributes.OUTPUT_VALUE, "".join(content_parts))

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
            finally:
                if span._span.is_recording():
                    span.finish_tracing()

        return streaming_wrapper()


class _EmbedWrapper(_WithTracer):
    """Wrapper for Ollama embeddings."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor()

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
        request_parameters["_endpoint"] = "embed"
        if args:
            request_parameters["model"] = args[0] if len(args) > 0 else kwargs.get("model")
            if len(args) > 1:
                request_parameters["input"] = args[1]

        span_name = f"ollama.embed"

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
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
    """Async wrapper for Ollama embeddings."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor()

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
        request_parameters["_endpoint"] = "embed"
        if args:
            request_parameters["model"] = args[0] if len(args) > 0 else kwargs.get("model")
            if len(args) > 1:
                request_parameters["input"] = args[1]

        span_name = f"ollama.embed"

        with self._start_as_current_span(
            span_name=span_name,
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
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
