import logging
from abc import ABC
from contextlib import contextmanager
from enum import Enum
from inspect import Signature, signature
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Tuple

import opentelemetry.context as context_api
from fi_instrumentation import get_attributes_from_context
from fi_instrumentation.fi_types import (
    EmbeddingAttributes,
    FiSpanKindValues,
    MessageAttributes,
    SpanAttributes,
)
from groq import NOT_GIVEN
from wrapt import ObjectProxy
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN
from opentelemetry.util.types import AttributeValue
from traceai_groq._request_attributes_extractor import _RequestAttributesExtractor
from traceai_groq._response_attributes_extractor import _ResponseAttributesExtractor
from traceai_groq._utils import _finish_tracing
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

    _media_dialect = "groq"

    def __init__(
        self,
        tracer: trace_api.Tracer,
        *args: Any,
        attachments: Any = None,
        config: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._tracer = tracer
        # Provider-owned shared attachment manager, or None (no capture, no imports).
        self._attachments = attachments
        self._config = config

    def _capture_chat_media(self, span: Any, messages: Any) -> None:
        """Typed chat media (data-URI images) through the shared attachment manager; never raises."""
        if self._attachments is None:
            return
        try:
            from fi_instrumentation.attachments.provider_media import ProviderMedia

            ProviderMedia(
                self._attachments, span, "groq", trace_config=self._config
            ).compatible_messages(messages, self._media_dialect)
        except Exception:
            logger.debug("Attachment capture unavailable", exc_info=True)

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
    # Binding must not serialize opaque clients or transport/auth configuration.
    # The extractor projects safe telemetry; the SDK still receives the original
    # args and kwargs unchanged.
    return {
        key: value
        for key, value in bound_signature.arguments.items()
        if value is not None and value is not NOT_GIVEN
        # This is a telemetry-only view. Never drain caller-owned messages.
        and not (key == "messages" and isinstance(value, Iterator))
    }


class _Stream(ObjectProxy):
    """Keep SDK lifecycle APIs while owning one invocation's tracing state."""

    def __init__(self, response, span, extractor, request_parameters):
        super().__init__(response)
        self._self_span = span
        self._self_extractor = extractor
        self._self_request_parameters = request_parameters
        self._self_chunks = []
        self._self_iterator = None
        self._self_finished = False

    def __del__(self):
        # Finalize abandoned tracing without reading or driving the transport.
        # The SDK still owns its explicit (possibly asynchronous) close API.
        try:
            self._finish()
        except Exception:
            pass

    def _finish(self, error=None, complete=False):
        if self._self_finished:
            return
        self._self_finished = True
        span = self._self_span
        try:
            if error is not None:
                span.record_exception(error)
                span.finish_tracing(status=trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(error).__name__}: {error}",
                ))
            elif complete:
                _finish_tracing(
                    status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                    with_span=span,
                    attributes=self._self_extractor.get_attributes(
                        response=self._self_chunks, is_streaming=True,
                    ),
                    extra_attributes=self._self_extractor.get_extra_attributes(
                        response=self._self_chunks,
                        request_parameters=self._self_request_parameters,
                        is_streaming=True,
                    ),
                )
            else:
                # Early close is not a completed response. Do not invent output
                # or an OK status for a stream the application did not exhaust.
                span.finish_tracing()
        except Exception:
            logger.exception("Failed to finalize streaming response")
            span.finish_tracing()
        finally:
            self._self_chunks.clear()


class _SyncStream(_Stream):
    def __iter__(self):
        return self

    def __next__(self):
        try:
            if self._self_iterator is None:
                self._self_iterator = iter(self.__wrapped__)
            item = next(self._self_iterator)
        except StopIteration:
            self._finish(complete=True)
            raise
        except BaseException as error:
            self._finish(error=error)
            raise
        if not self._self_finished:
            self._self_chunks.append(item)
        return item

    def close(self):
        try:
            if close := getattr(self.__wrapped__, "close", None):
                return close()
        except BaseException as error:
            self._finish(error=error)
            raise
        finally:
            self._finish()

    def __enter__(self):
        try:
            self.__wrapped__.__enter__()
        except BaseException as error:
            self._finish(error=error)
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            return self.__wrapped__.__exit__(exc_type, exc, traceback)
        except BaseException as error:
            self._finish(error=error)
            raise
        finally:
            # An exception in the consumer's context body is not an SDK error.
            self._finish()


class _AsyncStream(_Stream):
    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            if self._self_iterator is None:
                self._self_iterator = self.__wrapped__.__aiter__()
            item = await self._self_iterator.__anext__()
        except StopAsyncIteration:
            self._finish(complete=True)
            raise
        except BaseException as error:
            self._finish(error=error)
            raise
        if not self._self_finished:
            self._self_chunks.append(item)
        return item

    async def close(self):
        try:
            close = getattr(self.__wrapped__, "close", None)
            if close is None:
                close = getattr(self.__wrapped__, "aclose", None)
            if close is not None:
                return await close()
        except BaseException as error:
            self._finish(error=error)
            raise
        finally:
            self._finish()

    async def aclose(self):
        return await self.close()

    async def __aenter__(self):
        try:
            await self.__wrapped__.__aenter__()
        except BaseException as error:
            self._finish(error=error)
            raise
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            return await self.__wrapped__.__aexit__(exc_type, exc, traceback)
        except BaseException as error:
            self._finish(error=error)
            raise
        finally:
            # An exception in the consumer's context body is not an SDK error.
            self._finish()


class _CompletionsWrapper(_WithTracer):
    """
    Wrapper for the pipeline processing
    Captures all calls to the pipeline
    """

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
            self._capture_chat_media(span._span, kwargs.get("messages"))
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
                return _SyncStream(
                    response, span, self._response_extractor, request_parameters,
                )
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

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)

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
            self._capture_chat_media(span._span, kwargs.get("messages"))
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
                return _AsyncStream(
                    response, span, self._response_extractor, request_parameters,
                )
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
GEN_AI_OUTPUT_MESSAGES = SpanAttributes.GEN_AI_OUTPUT_MESSAGES
MESSAGE_CONTENT = MessageAttributes.MESSAGE_CONTENT
MESSAGE_ROLE = MessageAttributes.MESSAGE_ROLE
EMBEDDING_VECTOR = EmbeddingAttributes.EMBEDDING_VECTOR
EMBEDDING_TEXT = EmbeddingAttributes.EMBEDDING_TEXT
GEN_AI_USAGE_OUTPUT_TOKENS = SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS
