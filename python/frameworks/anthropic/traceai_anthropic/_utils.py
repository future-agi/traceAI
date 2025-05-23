import logging
from typing import Any, Iterator, NamedTuple, Optional, Protocol, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import FiMimeTypeValues, SpanAttributes
from opentelemetry import trace as trace_api
from opentelemetry.util.types import Attributes, AttributeValue
from traceai_anthropic._with_span import _WithSpan

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _ValueAndType(NamedTuple):
    value: str
    type: FiMimeTypeValues


class _HasAttributes(Protocol):
    def get_attributes(self) -> Iterator[Tuple[str, AttributeValue]]: ...

    def get_extra_attributes(self) -> Iterator[Tuple[str, AttributeValue]]: ...


def _finish_tracing(
    with_span: _WithSpan,
    has_attributes: _HasAttributes,
    status: Optional[trace_api.Status] = None,
) -> None:
    try:
        attributes: Attributes = dict(has_attributes.get_attributes())
    except Exception as e:
        logger.exception("Failed to get attributes")
        attributes = None

    try:
        extra_attributes: Attributes = dict(has_attributes.get_extra_attributes())
    except Exception as e:
        logger.exception("Failed to get extra attributes")
        extra_attributes = None

    try:
        with_span.finish_tracing(
            status=status,
            attributes=attributes,
            extra_attributes=extra_attributes,
        )
    except Exception as e:
        print(f"Failed to finish tracing: {e}")
        logger.exception("Failed to finish tracing")
        raise


def _io_value_and_type(obj: Any) -> _ValueAndType:
    try:
        json_value = safe_json_dumps(obj)
        return _ValueAndType(json_value, FiMimeTypeValues.JSON)
    except Exception as e:
        print(f"Failed to serialize as JSON: {e}")
        logger.exception("Failed to get input attributes from request parameters.")

    return _ValueAndType(str(obj), FiMimeTypeValues.TEXT)


def _as_input_attributes(
    value_and_type: Optional[_ValueAndType],
) -> Iterator[Tuple[str, AttributeValue]]:
    if not value_and_type:
        return

    yield SpanAttributes.INPUT_VALUE, value_and_type.value

    if value_and_type.type is not FiMimeTypeValues.TEXT:
        yield SpanAttributes.INPUT_MIME_TYPE, value_and_type.type.value


def _as_output_attributes(
    value_and_type: Optional[_ValueAndType],
) -> Iterator[Tuple[str, AttributeValue]]:
    if not value_and_type:
        return

    yield SpanAttributes.OUTPUT_VALUE, value_and_type.value

    if value_and_type.type is not FiMimeTypeValues.TEXT:
        yield SpanAttributes.OUTPUT_MIME_TYPE, value_and_type.type.value
