import logging
from abc import ABC
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Tuple

import opentelemetry.context as context_api
from fi_instrumentation import TraceConfig, get_attributes_from_context, safe_json_dumps
from fi_instrumentation.fi_types import SpanAttributes
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

    @staticmethod
    def _collect_attributes(
        attributes: Iterable[Tuple[str, AttributeValue]],
    ) -> Dict[str, AttributeValue]:
        collected: Dict[str, AttributeValue] = {}
        try:
            for key, value in attributes:
                collected[key] = value
        except Exception:
            logger.exception("Failed to extract request attributes")
        return collected

    @contextmanager
    def _start_as_current_span(
        self,
        span_name: str,
        attributes: Iterable[Tuple[str, AttributeValue]],
        context_attributes: Iterable[Tuple[str, AttributeValue]],
        extra_attributes: Iterable[Tuple[str, AttributeValue]],
    ) -> Iterator[_WithSpan]:
        # Attribute extraction is best effort, not a prerequisite for a span.
        # Keep each group independent and retain values yielded before a failure.
        initial_attributes = self._collect_attributes(extra_attributes)
        final_attributes = self._collect_attributes(attributes)
        span_context_attributes = self._collect_attributes(context_attributes)
        try:
            span = self._tracer.start_span(
                name=span_name, attributes=initial_attributes
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
                context_attributes=span_context_attributes,
                extra_attributes=final_attributes,
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

            return self._handle_streaming_response(span, response, request_parameters)

    def _handle_streaming_response(
        self, span: _WithSpan, response: Any, request_parameters: Dict[str, Any]
    ):
        """Handle streaming response."""
        content_parts: List[str] = []
        raw_chunks: List[Dict[str, Any]] = []
        final_response: Dict[str, Any] = {}

        def streaming_wrapper():
            nonlocal final_response
            try:
                for event in response:
                    event_dict = _to_dict(event)
                    raw_chunks.append(event_dict)

                    event_type = event_dict.get("event_type", "")

                    if event_type == "text-generation":
                        text = event_dict.get("text", "")
                        if text:
                            content_parts.append(text)

                    if event_type == "stream-end":
                        final_response = event_dict.get("response", {})

                    yield event

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
                    final_response["text"] = "".join(content_parts)
                    span._span.set_attribute(SpanAttributes.OUTPUT_VALUE, safe_json_dumps(raw_chunks[-5:]))  # Last 5 chunks
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


class _AsyncChatStreamWrapper(_ChatStreamWrapper):
    """Preserve the SDK's lazy async iterator and close its HTTP stream."""

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        response = wrapped(*args, **kwargs)
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return response
        return self._handle_async_stream(response, dict(kwargs))

    async def _handle_async_stream(
        self, response: Any, request_parameters: Dict[str, Any]
    ):
        # Start on first consumption, not construction. Closing an unstarted
        # stream therefore neither sends a request nor leaves an open span.
        with self._start_as_current_span(
            span_name="cohere.chat_stream",
            attributes=self._request_extractor.get_attributes_from_request(request_parameters),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._request_extractor.get_extra_attributes_from_request(request_parameters),
        ) as span:
            pass
        content_parts: List[str] = []
        final_response: Dict[str, Any] = {}
        try:
            try:
                iterator = response.__aiter__()
                while True:
                    # Never keep our context attached across a yield into
                    # application code (or across iteration in another task).
                    with trace_api.use_span(
                        span._span, end_on_exit=False,
                        record_exception=False, set_status_on_exception=False,
                    ):
                        try:
                            event = await iterator.__anext__()
                        except StopAsyncIteration:
                            break
                    try:
                        event_dict = _to_dict(event)
                        if event_dict.get("event_type") == "text-generation":
                            if text := event_dict.get("text"):
                                content_parts.append(text)
                        elif event_dict.get("event_type") == "stream-end":
                            final_response = dict(event_dict.get("response") or {})
                    except Exception:
                        logger.exception("Failed to extract streaming response")
                    yield event
            finally:
                # Explicit close, cancellation and upstream failures must all
                # release the actual SDK generator's HTTP context immediately.
                with trace_api.use_span(
                    span._span, end_on_exit=False,
                    record_exception=False, set_status_on_exception=False,
                ):
                    await response.aclose()
        except Exception as exception:
            span.record_exception(exception)
            span.finish_tracing(status=trace_api.Status(
                status_code=trace_api.StatusCode.ERROR,
                description=f"{type(exception).__name__}: {exception}",
            ))
            raise
        else:
            try:
                final_response["text"] = "".join(content_parts)
                # Legacy provider-specific tool fields do not pass through
                # the core's canonical output-message masking rules.
                if (self._config.hide_outputs or self._config.hide_output_messages
                        or self._config.hide_output_text):
                    final_response.pop("tool_calls", None)
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
            # GeneratorExit and CancelledError also take this path. They do not
            # claim that a partially consumed stream completed successfully.
            span.finish_tracing()


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
