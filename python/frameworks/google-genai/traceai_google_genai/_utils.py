import logging
import warnings
from typing import Any, Iterable, Iterator, Mapping, NamedTuple, Optional, Sequence, Tuple

from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue

from fi_instrumentation import safe_json_dumps
from traceai_google_genai._with_span import _WithSpan
from fi_instrumentation.fi_types import FiMimeTypeValues, SpanAttributes

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _ValueAndType(NamedTuple):
    value: str
    type: FiMimeTypeValues


def _io_value_and_type(obj: Any) -> _ValueAndType:
    if hasattr(obj, "model_dump_json") and callable(obj.model_dump_json):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                # `warnings=False` in `model_dump_json()` is only supported in Pydantic v2
                value = obj.model_dump_json(exclude_unset=True)
            assert isinstance(value, str)
        except Exception:
            logger.exception("Failed to get model dump json")
        else:
            return _ValueAndType(value, FiMimeTypeValues.JSON)
    if not isinstance(obj, str) and isinstance(obj, (Sequence, Mapping)):
        try:
            value = safe_json_dumps(obj)
        except Exception:
            logger.exception("Failed to dump json")
        else:
            return _ValueAndType(value, FiMimeTypeValues.JSON)

    return _ValueAndType(str(obj), FiMimeTypeValues.TEXT)


# Response-side fields that may be recorded while input settings are on. The request echo
# (automatic_function_calling_history, the contents sent to the model) is left out rather than stripped by name.
_RESPONSE_FIELDS = ("candidates", "usage_metadata", "model_version", "response_id", "create_time")


def _input_hidden(tracer: Any) -> bool:
    config = getattr(tracer, "_self_config", None)
    return bool(config is not None and (config.hide_inputs or config.hide_input_messages or config.hide_input_text or config.hide_input_images))


def _response_view(response: Any, tracer: Any) -> Any:
    """The response for the whole-output dump; under any input hide setting, only its response-side fields."""
    if not _input_hidden(tracer) or not (hasattr(response, "model_dump") and callable(response.model_dump)):
        return response
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            dumped = response.model_dump(exclude_unset=True, mode="json")
        return {k: dumped[k] for k in _RESPONSE_FIELDS if k in dumped}
    except Exception:
        logger.exception("Failed to build the response-side view")
        return {}


def _as_input_attributes(
    value_and_type: Optional[_ValueAndType],
) -> Iterator[Tuple[str, AttributeValue]]:
    if not value_and_type:
        return
    yield SpanAttributes.INPUT_VALUE, value_and_type.value
    # It's assumed to be TEXT by default, so we can skip to save one attribute.
    if value_and_type.type is not FiMimeTypeValues.TEXT:
        yield SpanAttributes.INPUT_MIME_TYPE, value_and_type.type.value


def _as_output_attributes(
    value_and_type: Optional[_ValueAndType],
) -> Iterator[Tuple[str, AttributeValue]]:
    if not value_and_type:
        return
    yield SpanAttributes.OUTPUT_VALUE, value_and_type.value
    # It's assumed to be TEXT by default, so we can skip to save one attribute.
    if value_and_type.type is not FiMimeTypeValues.TEXT:
        yield SpanAttributes.OUTPUT_MIME_TYPE, value_and_type.type.value


def _finish_tracing(
    with_span: _WithSpan,
    attributes: Iterable[Tuple[str, AttributeValue]],
    extra_attributes: Iterable[Tuple[str, AttributeValue]],
    status: Optional[trace_api.Status] = None,
) -> None:
    try:
        attributes_dict = dict(attributes)
    except Exception:
        logger.exception("Failed to get attributes")
    try:
        extra_attributes_dict = dict(extra_attributes)
    except Exception:
        logger.exception("Failed to get extra attributes")
    try:
        with_span.finish_tracing(
            status=status,
            attributes=attributes_dict,
            extra_attributes=extra_attributes_dict,
        )
    except Exception:
        logger.exception("Failed to finish tracing")
