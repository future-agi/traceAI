from __future__ import annotations

from typing import Any, Callable, ContextManager, Iterator, Mapping, Optional, cast

import wrapt
from botocore.eventstream import EventStream
from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    FiMimeTypeValues,
    FiSpanKindValues,
    ImageAttributes,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
)
from opentelemetry.trace import Span, Status, StatusCode, use_span
from opentelemetry.util.types import AttributeValue
from traceai_bedrock._proxy import _AnyT, _CallbackT, _Iterator


class _EventStream(wrapt.ObjectProxy):  # type: ignore[misc]
    __wrapped__: EventStream

    def __init__(
        self,
        obj: EventStream,
        callback: Optional[_CallbackT[_AnyT]] = None,
        context_manager_factory: Optional[Callable[[], ContextManager[Any]]] = None,
    ) -> None:
        super().__init__(obj)
        self._self_callback = callback
        self._self_context_manager_factory = context_manager_factory

    def __iter__(self) -> Iterator[Any]:
        return _Iterator(
            iter(self.__wrapped__),
            self._self_callback,
            self._self_context_manager_factory,
        )


def _use_span(span: Span) -> Callable[[], ContextManager[Span]]:
    # The `use_span` context manager can't be entered more than once. It would err here:
    # https://github.com/open-telemetry/opentelemetry-python/blob/b1e99c1555721f818e578d7457587693e767e182/opentelemetry-api/src/opentelemetry/util/_decorator.py#L56  # noqa E501
    # So we need a factory.
    return lambda: cast(ContextManager[Span], use_span(span, False, False, False))


def _finish(
    span: Span,
    result: Any,
    request_attributes: Mapping[str, AttributeValue],
) -> None:
    if isinstance(result, BaseException):
        span.record_exception(result)
        span.set_status(Status(StatusCode.ERROR, f"{type(result).__name__}: {result}"))
        for k, v in request_attributes.items():
            span.set_attribute(k, v)
        span.end()
        return

    output = result["content"][0]["text"] if "content" in result else result
    span.set_attribute(OUTPUT_VALUE, safe_json_dumps(output))
    span.set_attribute(OUTPUT_MIME_TYPE, FiMimeTypeValues.TEXT.value)

    span.set_status(Status(StatusCode.OK))
    for k, v in request_attributes.items():
        span.set_attribute(k, v)
    span.end()


IMAGE_URL = ImageAttributes.IMAGE_URL
INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
INPUT_VALUE = SpanAttributes.INPUT_VALUE
JSON = FiMimeTypeValues.JSON.value
LLM = FiSpanKindValues.LLM.value
LLM_INPUT_MESSAGES = SpanAttributes.LLM_INPUT_MESSAGES
LLM_INVOCATION_PARAMETERS = SpanAttributes.LLM_INVOCATION_PARAMETERS
LLM_MODEL_NAME = SpanAttributes.LLM_MODEL_NAME
LLM_TOKEN_COUNT_COMPLETION = SpanAttributes.LLM_TOKEN_COUNT_COMPLETION
LLM_TOKEN_COUNT_PROMPT = SpanAttributes.LLM_TOKEN_COUNT_PROMPT
LLM_TOKEN_COUNT_TOTAL = SpanAttributes.LLM_TOKEN_COUNT_TOTAL
MESSAGE_CONTENT = MessageAttributes.MESSAGE_CONTENT
MESSAGE_CONTENT_IMAGE = MessageContentAttributes.MESSAGE_CONTENT_IMAGE
MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON = (
    MessageAttributes.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON
)
MESSAGE_FUNCTION_CALL_NAME = MessageAttributes.MESSAGE_FUNCTION_CALL_NAME
MESSAGE_NAME = MessageAttributes.MESSAGE_NAME
FI_SPAN_KIND = SpanAttributes.FI_SPAN_KIND
OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
