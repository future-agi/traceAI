import logging
from abc import ABC
from contextlib import contextmanager
from enum import Enum
from inspect import Signature, signature
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Tuple,
)

import opentelemetry.context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN
from opentelemetry.util.types import AttributeValue

from fi_instrumentation import get_attributes_from_context
from traceai_portkey._request_attributes_extractor import (
    _RequestAttributesExtractor,
)
from traceai_portkey._response_attributes_extractor import (
    _ResponseAttributesExtractor,
)
from traceai_portkey._utils import _finish_tracing
from traceai_portkey._with_span import _WithSpan
from traceai_portkey._stream import _Stream, _AsyncStream, _suppress_nested

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
    """Base class for wrappers that use a tracer."""

    def __init__(self, tracer: trace_api.Tracer, *args: Any, **kwargs: Any) -> None:
        self._tracer = tracer
        super().__init__(*args, **kwargs)

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
    """Parse args and kwargs into a dictionary of parameter names to values."""
    bound_args = signature.bind(*args, **kwargs)
    bound_args.apply_defaults()
    return dict(bound_args.arguments)


class _CompletionsWrapper(_WithTracer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_extractor = _RequestAttributesExtractor()
        self._response_extractor = _ResponseAttributesExtractor()

    def _start(self, wrapped, args, kwargs) -> ContextManager[_WithSpan]:
        try:
            parameters = dict(signature(wrapped).bind(*args, **kwargs).arguments)
            parameters.update(parameters.pop("kwargs", {}))
        except (TypeError, ValueError):
            parameters = dict(kwargs)
        # Only inference parameters belong in telemetry, not transport credentials
        # (extra_headers, config, API keys, query strings or client objects).
        allowed = {
            "model",
            "messages",
            "prompt_id",
            "variables",
            "stream",
            "temperature",
            "max_tokens",
            "max_completion_tokens",
            "top_k",
            "top_p",
            "tools",
            "tool_choice",
            "functions",
            "function_call",
            "response_format",
            "seed",
            "stop",
            "frequency_penalty",
            "presence_penalty",
            "n",
            "stream_options",
        }
        # Leave one-shot iterables for the SDK; tracing must not consume messages/tools.
        parameters = {
            k: v
            for k, v in parameters.items()
            if k in allowed and not isinstance(v, Iterator)
        }
        try:
            attributes = dict(
                self._request_extractor.get_attributes_from_request(parameters)
            )
            extra = dict(
                self._request_extractor.get_extra_attributes_from_request(parameters)
            )
        except Exception as error:
            logger.warning(
                "Failed to extract Portkey request attributes (%s)",
                type(error).__name__,
            )
            attributes, extra = {}, {}
        return self._start_as_current_span(
            span_name="Completions",
            attributes=attributes,
            context_attributes=get_attributes_from_context(),
            extra_attributes=extra,
        )

    def _finish(self, span: _WithSpan, response: Any, status: trace_api.Status) -> None:
        _finish_tracing(
            with_span=span,
            attributes=self._response_extractor.get_attributes(response),
            extra_attributes=self._response_extractor.get_extra_attributes(
                response, {}
            ),
            status=status,
        )

    def _error(self, span: _WithSpan, exception: BaseException) -> None:
        span.record_exception(exception)
        span.finish_tracing(
            status=trace_api.Status(
                trace_api.StatusCode.ERROR, f"{type(exception).__name__}: {exception}"
            )
        )

    def _response(self, span: _WithSpan, response: Any) -> Any:
        if hasattr(response, "__anext__"):
            return _AsyncStream(response, span, self._finish)
        if hasattr(response, "__next__"):
            return _Stream(response, span, self._finish)
        self._finish(span, response, trace_api.Status(trace_api.StatusCode.OK))
        return response

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        with self._start(wrapped, args, kwargs) as span:
            try:
                with _suppress_nested():
                    response = wrapped(*args, **kwargs)
            except BaseException as exception:
                self._error(span, exception)
                raise
            return self._response(span, response)


class _AsyncCompletionsWrapper(_CompletionsWrapper):
    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)
        with self._start(wrapped, args, kwargs) as span:
            try:
                with _suppress_nested():
                    response = await wrapped(*args, **kwargs)
            except BaseException as exception:
                self._error(span, exception)
                raise
            return self._response(span, response)
