import logging
from abc import ABC
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, Mapping, Tuple

import opentelemetry.context as context_api
from fi_instrumentation import TraceConfig, get_attributes_from_context
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN
from opentelemetry.util.types import AttributeValue

from traceai_ollama._request_attributes_extractor import _RequestAttributesExtractor
from traceai_ollama._response_attributes_extractor import _ResponseAttributesExtractor
from traceai_ollama._stream import _AsyncStream, _StreamState, _SyncStream
from traceai_ollama._utils import _finish_tracing, _to_dict
from traceai_ollama._with_span import _WithSpan

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
    """Wrapper for Ollama chat completions."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor(config=self._config)

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

            if isinstance(response, Iterator):
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
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ) -> Any:
        return _SyncStream(
            response,
            _StreamState(span, self._response_extractor, request_parameters, self._config),
        )


class _AsyncChatWrapper(_WithTracer):
    """Async wrapper for Ollama chat completions."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor(config=self._config)

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

            if hasattr(response, "__aiter__"):
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

    def _handle_async_streaming_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ) -> Any:
        return _AsyncStream(
            response,
            _StreamState(span, self._response_extractor, request_parameters, self._config),
        )


class _GenerateWrapper(_WithTracer):
    """Wrapper for Ollama generate endpoint."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor(config=self._config)

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

            if isinstance(response, Iterator):
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
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ) -> Any:
        return _SyncStream(
            response,
            _StreamState(span, self._response_extractor, request_parameters, self._config),
        )


class _AsyncGenerateWrapper(_WithTracer):
    """Async wrapper for Ollama generate endpoint."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor(config=self._config)

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

            if hasattr(response, "__aiter__"):
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

    def _handle_async_streaming_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ) -> Any:
        return _AsyncStream(
            response,
            _StreamState(span, self._response_extractor, request_parameters, self._config),
        )


class _EmbedWrapper(_WithTracer):
    """Wrapper for Ollama embeddings."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor(config=self._config)

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
            except BaseException as exception:
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
        self._response_extractor = _ResponseAttributesExtractor(config=self._config)

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
            except BaseException as exception:
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
