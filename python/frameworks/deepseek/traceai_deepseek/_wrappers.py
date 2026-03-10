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

from traceai_deepseek._request_attributes_extractor import (
    _ChatCompletionRequestAttributesExtractor,
)
from traceai_deepseek._response_attributes_extractor import (
    _ChatCompletionResponseAttributesExtractor,
    _StreamingChatCompletionResponseExtractor,
)
from traceai_deepseek._utils import _finish_tracing, _to_dict, is_deepseek_client
from traceai_deepseek._with_span import _WithSpan

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


class _ChatCompletionWrapper(_WithTracer):
    """Wrapper for DeepSeek chat completions (via OpenAI client)."""

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

        # Check if this is a DeepSeek client
        # Get the parent client from the completions instance
        client = getattr(instance, "_client", None)
        if client is None:
            # Try to get from chat.completions structure
            chat = getattr(instance, "_parent", None) or getattr(instance, "chat", None)
            if chat:
                client = getattr(chat, "_client", None)

        if not is_deepseek_client(client) and not is_deepseek_client(instance):
            return wrapped(*args, **kwargs)

        request_parameters = dict(kwargs)
        is_streaming = request_parameters.get("stream", False)

        span_name = "deepseek.chat.completions"

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

            if is_streaming:
                return self._handle_streaming_response(span, response, request_parameters)

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
    ):
        """Handle streaming response."""
        stream_extractor = _StreamingChatCompletionResponseExtractor()

        def streaming_wrapper():
            try:
                for chunk in response:
                    chunk_dict = _to_dict(chunk)
                    stream_extractor.process_chunk(chunk_dict)
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
                    _finish_tracing(
                        status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                        with_span=span,
                        attributes=stream_extractor.get_attributes(),
                        extra_attributes=stream_extractor.get_extra_attributes(request_parameters),
                    )
                except Exception:
                    logger.exception("Failed to finalize streaming response")
                    span.finish_tracing()
            finally:
                if span._span.is_recording():
                    span.finish_tracing()

        return streaming_wrapper()


class _AsyncChatCompletionWrapper(_WithTracer):
    """Async wrapper for DeepSeek chat completions."""

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

        # Check if this is a DeepSeek client
        client = getattr(instance, "_client", None)
        if client is None:
            chat = getattr(instance, "_parent", None) or getattr(instance, "chat", None)
            if chat:
                client = getattr(chat, "_client", None)

        if not is_deepseek_client(client) and not is_deepseek_client(instance):
            return await wrapped(*args, **kwargs)

        request_parameters = dict(kwargs)
        is_streaming = request_parameters.get("stream", False)

        span_name = "deepseek.chat.completions"

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

            if is_streaming:
                return self._handle_async_streaming_response(span, response, request_parameters)

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
    ):
        """Handle async streaming response."""
        stream_extractor = _StreamingChatCompletionResponseExtractor()

        async def async_streaming_wrapper():
            try:
                async for chunk in response:
                    chunk_dict = _to_dict(chunk)
                    stream_extractor.process_chunk(chunk_dict)
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
                    _finish_tracing(
                        status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                        with_span=span,
                        attributes=stream_extractor.get_attributes(),
                        extra_attributes=stream_extractor.get_extra_attributes(request_parameters),
                    )
                except Exception:
                    logger.exception("Failed to finalize async streaming response")
                    span.finish_tracing()
            finally:
                if span._span.is_recording():
                    span.finish_tracing()

        return async_streaming_wrapper()
