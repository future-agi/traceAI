import logging
from abc import ABC
from contextlib import contextmanager
from itertools import chain
from types import ModuleType
from typing import Any, Awaitable, Callable, Iterable, Iterator, Mapping, Tuple

from fi_instrumentation import get_attributes_from_context
from fi_instrumentation.fi_types import (
    FiLLMProviderValues,
    FiLLMSystemValues,
    FiSpanKindValues,
    SpanAttributes,
)
from openai._legacy_response import LegacyAPIResponse
from openai.types.chat.chat_completion import ChatCompletion
from openai.types.create_embedding_response import CreateEmbeddingResponse
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import INVALID_SPAN
from opentelemetry.util.types import AttributeValue
from traceai_openai._request_attributes_extractor import _RequestAttributesExtractor
from traceai_openai._response_accumulator import (
    _ChatCompletionAccumulator,
    _CompletionAccumulator,
)
from traceai_openai._response_attributes_extractor import _ResponseAttributesExtractor
from traceai_openai._span_io_handler import add_io_to_span_attributes
from traceai_openai._stream import _AsyncStream, _ResponseAccumulator, _Stream
from traceai_openai._utils import (
    _as_input_attributes,
    _as_output_attributes,
    _finish_tracing,
    _io_value_and_type,
    safe_json_dumps,
)
from traceai_openai._with_span import _WithSpan
from typing_extensions import TypeAlias

__all__ = (
    "_Request",
    "_AsyncRequest",
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _WithTracer(ABC):
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
        # Because OTEL has a default limit of 128 attributes, we split our attributes into
        # two tiers, where the addition of "extra_attributes" is deferred until the end
        # and only after the "attributes" are added.
        try:
            span = self._tracer.start_span(name=span_name, attributes=dict(attributes))
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
                extra_attributes=dict(extra_attributes),
            )


_RequestParameters: TypeAlias = Mapping[str, Any]


class _WithOpenAI(ABC):
    __slots__ = (
        "_openai",
        "_stream_types",
        "_request_attributes_extractor",
        "_response_attributes_extractor",
        "_response_accumulator_factories",
    )

    def __init__(self, openai: ModuleType, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._openai = openai
        self._stream_types = (openai.Stream, openai.AsyncStream)
        self._request_attributes_extractor = _RequestAttributesExtractor(openai=openai)
        self._response_attributes_extractor = _ResponseAttributesExtractor(
            openai=openai
        )
        self._response_accumulator_factories: Mapping[
            type, Callable[[_RequestParameters], _ResponseAccumulator]
        ] = {
            openai.types.Completion: lambda request_parameters: _CompletionAccumulator(
                request_parameters=request_parameters,
                completion_type=openai.types.Completion,
                response_attributes_extractor=self._response_attributes_extractor,
            ),
            openai.types.chat.ChatCompletion: lambda request_parameters: _ChatCompletionAccumulator(
                request_parameters=request_parameters,
                chat_completion_type=openai.types.chat.ChatCompletion,
                response_attributes_extractor=self._response_attributes_extractor,
            ),
        }

    def _get_span_kind(self, cast_to: type) -> str:
        return (
            FiSpanKindValues.EMBEDDING.value
            if cast_to is self._openai.types.CreateEmbeddingResponse
            else FiSpanKindValues.LLM.value
        )

    def _get_attributes_from_instance(
        self, instance: Any
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if (
            not (base_url := getattr(instance, "base_url", None))
            or not (host := getattr(base_url, "host", None))
            or not isinstance(host, str)
        ):
            return
        if host.endswith("api.openai.com"):
            yield SpanAttributes.LLM_PROVIDER, FiLLMProviderValues.OPENAI.value
        elif host.endswith("openai.azure.com"):
            yield SpanAttributes.LLM_PROVIDER, FiLLMProviderValues.AZURE.value
        elif host.endswith("googleapis.com"):
            yield SpanAttributes.LLM_PROVIDER, FiLLMProviderValues.GOOGLE.value

    def _get_attributes_from_request(
        self,
        cast_to: type,
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:
        yield SpanAttributes.FI_SPAN_KIND, self._get_span_kind(cast_to=cast_to)
        yield SpanAttributes.LLM_SYSTEM, FiLLMSystemValues.OPENAI.value
        try:
            yield from _as_input_attributes(
                _io_value_and_type(request_parameters),
            )
        except Exception:
            logger.exception(
                f"Failed to get input attributes from request parameters of "
                f"type {type(request_parameters)}"
            )

    def _get_extra_attributes_from_request(
        self,
        cast_to: type,
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:
        # Secondary attributes should be added after input and output to ensure
        # that input and output are not dropped if there are too many attributes.
        try:
            yield from self._request_attributes_extractor.get_attributes_from_request(
                cast_to=cast_to,
                request_parameters=request_parameters,
            )
        except Exception:
            logger.exception(
                f"Failed to get extra attributes from request options of "
                f"type {type(request_parameters)}"
            )

    def _is_streaming(self, response: Any) -> bool:
        return isinstance(response, self._stream_types)

    def _is_async_stream(self, response: Any) -> bool:
        """Check if the response is an asynchronous stream."""
        return hasattr(response, "__aiter__") and callable(response.__aiter__)

    def _finalize_response(
        self,
        response: Any,
        with_span: _WithSpan,
        cast_to: type,
        request_parameters: Mapping[str, Any],
    ) -> Any:
        """
        Monkey-patch the response object to trace the stream, or finish tracing if the response is
        not a stream.
        """
        if hasattr(response, "parse") and callable(response.parse):
            try:
                response.parse()
            except Exception:
                logger.exception(f"Failed to parse response of type {type(response)}")

        if self._is_streaming(response):
            # Determine if the response is asynchronous
            if self._is_async_stream(response):
                stream_wrapper = _AsyncStream
            else:
                stream_wrapper = _Stream

            # Get the response accumulator if available
            response_accumulator_factory = self._response_accumulator_factories.get(
                cast_to
            )
            response_accumulator = (
                response_accumulator_factory(request_parameters)
                if response_accumulator_factory
                else None
            )

            # Wrap the response with the appropriate stream wrapper
            wrapped_stream = stream_wrapper(
                stream=response,
                with_span=with_span,
                response_accumulator=response_accumulator,
            )

            return wrapped_stream

        else:
            _finish_tracing(
                status=trace_api.Status(status_code=trace_api.StatusCode.OK),
                with_span=with_span,
                has_attributes=_ResponseAttributes(
                    request_parameters=request_parameters,
                    response=response,
                    response_attributes_extractor=self._response_attributes_extractor,
                ),
            )
            return response

    def response_to_dict(self, response: Any) -> Any:
        if hasattr(response, "to_dict") and callable(response.to_dict):
            return response.to_dict()
        elif isinstance(response, dict):
            return response
        elif hasattr(response, "__dict__"):
            return response.__dict__
        else:
            return str(response)


class _Request(_WithTracer, _WithOpenAI):
    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[type, Any],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        try:
            cast_to, request_parameters = _parse_request_args(args)
            # Extract input data more robustly
            input_data = (
                request_parameters.get("messages")
                or request_parameters.get("prompt")
                or request_parameters.get("input")
            )
            llm_tools = request_parameters.get("tools")
            span_name: str = cast_to.__name__.split(".")[-1]
        except Exception:
            logger.exception("Failed to parse request args")
            return wrapped(*args, **kwargs)

        with self._start_as_current_span(
            span_name=span_name,
            attributes=chain(
                self._get_attributes_from_instance(instance),
                self._get_attributes_from_request(
                    cast_to=cast_to,
                    request_parameters=request_parameters,
                ),
            ),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._get_extra_attributes_from_request(
                cast_to=cast_to,
                request_parameters=request_parameters,
            ),
        ) as with_span:
            # Add input data to span attributes before the request
            with_span.set_attribute(
                SpanAttributes.RAW_INPUT, safe_json_dumps(request_parameters)
            )
            with_span.set_attribute(
                SpanAttributes.LLM_TOOLS, safe_json_dumps(llm_tools)
            )
            add_io_to_span_attributes(with_span, input_data, None)
            try:
                response = wrapped(*args, **kwargs)

                if isinstance(response, CreateEmbeddingResponse):
                    embedding = response.to_dict()
                    with_span.set_attribute(
                        SpanAttributes.EMBEDDING_EMBEDDINGS, safe_json_dumps(embedding)
                    )

                # Add output data to span attributes after getting response
                if not self._is_streaming(response):
                    with_span.set_attribute(
                        SpanAttributes.RAW_OUTPUT, _get_raw_output(response)
                    )
                    add_io_to_span_attributes(
                        with_span,
                        None,
                        response,
                        is_streaming=self._is_streaming(response),
                    )
                return self._finalize_response(
                    response=response,
                    with_span=with_span,
                    cast_to=cast_to,
                    request_parameters=request_parameters,
                )
            except Exception as e:
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    description=f"{type(e).__name__}: {e}",
                )
                with_span.record_exception(e)
                with_span.finish_tracing(status=status)
                raise


class _AsyncRequest(_WithTracer, _WithOpenAI):
    async def __call__(
        self,
        wrapped: Callable[..., Awaitable[Any]],
        instance: Any,
        args: Tuple[type, Any],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)
        try:
            cast_to, request_parameters = _parse_request_args(args)
            # Extract input data more robustly
            input_data = request_parameters.get(
                "messages", request_parameters.get("prompt")
            )
            llm_tools = request_parameters.get("tools")

            span_name: str = cast_to.__name__.split(".")[-1]
        except Exception:
            logger.exception("Failed to parse request args")
            return await wrapped(*args, **kwargs)
        with self._start_as_current_span(
            span_name=span_name,
            attributes=chain(
                self._get_attributes_from_instance(instance),
                self._get_attributes_from_request(
                    cast_to=cast_to,
                    request_parameters=request_parameters,
                ),
            ),
            context_attributes=get_attributes_from_context(),
            extra_attributes=self._get_extra_attributes_from_request(
                cast_to=cast_to,
                request_parameters=request_parameters,
            ),
        ) as with_span:
            # Add input data to span attributes before the request
            with_span.set_attribute(
                SpanAttributes.RAW_INPUT, safe_json_dumps(request_parameters)
            )
            with_span.set_attribute(
                SpanAttributes.LLM_TOOLS, safe_json_dumps(llm_tools)
            )
            add_io_to_span_attributes(with_span, input_data, None)
            try:
                response = await wrapped(*args, **kwargs)

                if isinstance(response, CreateEmbeddingResponse):
                    embedding = response.to_dict()
                    with_span.set_attribute(
                        SpanAttributes.EMBEDDING_EMBEDDINGS, safe_json_dumps(embedding)
                    )

                if not self._is_streaming(response):
                    with_span.set_attribute(
                        SpanAttributes.RAW_OUTPUT, _get_raw_output(response)
                    )

                add_io_to_span_attributes(
                    with_span, None, response, is_streaming=self._is_streaming(response)
                )
            except Exception as exception:
                with_span.record_exception(exception)
                status = trace_api.Status(
                    status_code=trace_api.StatusCode.ERROR,
                    # Follow the format in OTEL SDK for description, see:
                    # https://github.com/open-telemetry/opentelemetry-python/blob/2b9dcfc5d853d1c10176937a6bcaade54cda1a31/opentelemetry-api/src/opentelemetry/trace/__init__.py#L588  # noqa E501
                    description=f"{type(exception).__name__}: {exception}",
                )
                with_span.finish_tracing(status=status)
                raise
            try:
                response = self._finalize_response(
                    response=response,
                    with_span=with_span,
                    cast_to=cast_to,
                    request_parameters=request_parameters,
                )
            except Exception:
                logger.exception(
                    f"Failed to finalize response of type {type(response)}"
                )
                with_span.finish_tracing()
        return response


def _parse_request_args(args: Tuple[type, Any]) -> Tuple[type, Mapping[str, Any]]:
    # We don't use `signature(request).bind()` because `request` could have been monkey-patched
    # (incorrectly) by others and the signature at runtime may not match the original.
    # The targeted signature of `request` is here:
    # https://github.com/openai/openai-python/blob/f1c7d714914e3321ca2e72839fe2d132a8646e7f/src/openai/_base_client.py#L846-L847  # noqa: E501
    cast_to: type = args[0]
    request_parameters: Mapping[str, Any] = (
        json_data
        # See https://github.com/openai/openai-python/blob/f1c7d714914e3321ca2e72839fe2d132a8646e7f/src/openai/_models.py#L427  # noqa: E501
        if hasattr(args[1], "json_data")
        and isinstance(json_data := args[1].json_data, Mapping)
        else {}
    )
    # FIXME: Because request parameters is just a Mapping, it can contain any value as long as it
    # serializes correctly in an HTTP request body. For example, Enum values may be present if a
    # third-party library puts them there. Enums can turn into their intended string values via
    # `json.dumps` when the final HTTP request body is serialized, but can pose problems when we
    # try to extract attributes. However, this round-trip seems expensive, so we opted to treat
    # only the Enums that we know about: e.g. message role sometimes can be an Enum, so we will
    # convert it only when it's encountered.
    # try:
    #     request_parameters = json.loads(json.dumps(request_parameters))
    # except Exception:
    #     pass
    return cast_to, request_parameters


class _ResponseAttributes:
    __slots__ = ("_response", "_request_parameters", "_response_attributes_extractor")

    def __init__(
        self,
        response: Any,
        request_parameters: Mapping[str, Any],
        response_attributes_extractor: _ResponseAttributesExtractor,
    ) -> None:
        if hasattr(response, "parse") and callable(response.parse):
            # E.g. see https://github.com/openai/openai-python/blob/f1c7d714914e3321ca2e72839fe2d132a8646e7f/src/openai/_base_client.py#L518  # noqa: E501
            try:
                response = response.parse()
            except Exception:
                logger.exception(f"Failed to parse response of type {type(response)}")
        self._request_parameters = request_parameters
        self._response = response
        self._response_attributes_extractor = response_attributes_extractor

    def get_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        yield from _as_output_attributes(
            _io_value_and_type(self._response),
        )

    def get_extra_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        yield from self._response_attributes_extractor.get_attributes_from_response(
            response=self._response,
            request_parameters=self._request_parameters,
        )


def _get_raw_output(response):
    if isinstance(response, ChatCompletion):
        return safe_json_dumps(response.to_dict())
    elif isinstance(response, LegacyAPIResponse):
        parsed_response = response.parse()
        if hasattr(parsed_response, "dict") and callable(parsed_response.dict):
            parsed_response = parsed_response.dict()
            return safe_json_dumps(parsed_response)
        else:
            return safe_json_dumps(str(parsed_response))
    elif hasattr(response, "to_dict") and callable(response.to_dict):
        return safe_json_dumps(response.to_dict())
    else:
        return safe_json_dumps(str(response))
