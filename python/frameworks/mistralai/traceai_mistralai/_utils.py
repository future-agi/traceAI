import logging
from typing import Any, Iterator, NamedTuple, Optional, Protocol, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import FiMimeTypeValues, SpanAttributes
from opentelemetry import trace as trace_api
from opentelemetry.util.types import Attributes, AttributeValue
from traceai_mistralai._with_span import _WithSpan

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
    except Exception:
        logger.exception("Failed to get attributes")
        attributes = None
    try:
        extra_attributes: Attributes = dict(has_attributes.get_extra_attributes())
    except Exception:
        logger.exception("Failed to get extra attributes")
        extra_attributes = None
    try:
        with_span.finish_tracing(
            status=status,
            attributes=attributes,
            extra_attributes=extra_attributes,
        )
    except Exception:
        logger.exception("Failed to finish tracing")


def _io_value_and_type(obj: Any) -> _ValueAndType:
    try:
        return _ValueAndType(safe_json_dumps(obj), FiMimeTypeValues.JSON)
    except Exception:
        logger.exception("Failed to get input attributes from request parameters.")
    return _ValueAndType(str(obj), FiMimeTypeValues.TEXT)


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


def _process_input_messages(messages: list) -> Iterator[Tuple[str, AttributeValue]]:
    try:
        if not messages:
            return

        input_images = []
        filtered_messages = []
        eval_input = []

        for message in messages:
            content = message.get("content")

            if isinstance(content, str):
                filtered_messages.append(message)
                eval_input.append(content)
                continue

            if isinstance(content, list):
                filtered_content = []
                for item in content:
                    if isinstance(item, dict):
                        if "image_url" in item:
                            input_images.append(item["image_url"])
                            image_index = len(input_images) - 1
                            eval_input.append(
                                f"{{{SpanAttributes.INPUT_IMAGES}.{image_index}}}"
                            )
                        elif item.get("type") == "text":
                            filtered_content.append(item)
                            eval_input.append(item.get("text", ""))

                if filtered_content:
                    filtered_msg = message.copy()
                    filtered_msg["content"] = filtered_content
                    filtered_messages.append(filtered_msg)

        if input_images:
            yield SpanAttributes.INPUT_IMAGES, safe_json_dumps(input_images)
        if filtered_messages:
            yield SpanAttributes.INPUT_VALUE, safe_json_dumps(filtered_messages)
            yield SpanAttributes.INPUT_MIME_TYPE, FiMimeTypeValues.JSON.value
        if eval_input:
            yield SpanAttributes.EVAL_INPUT, safe_json_dumps("\n".join(eval_input))
            yield SpanAttributes.QUERY, safe_json_dumps(eval_input[0])

    except Exception as e:
        print(f"Error in _process_input_messages: {str(e)}")
        return


def _raw_input(input: Any) -> Iterator[Tuple[str, AttributeValue]]:
    if not input:
        return

    yield SpanAttributes.RAW_INPUT, safe_json_dumps(input)


async def _process_streaming_response(response: list) -> tuple[str, str]:
    chunks = []
    full_content = []

    async for chunk in response:
        processed_chunk = _to_dict(chunk)
        chunks.append(processed_chunk)
        if processed_chunk and "data" in processed_chunk:
            chunk_data = processed_chunk["data"]
            if "choices" in chunk_data and chunk_data["choices"]:
                choice = chunk_data["choices"][0]
                if "delta" in choice and "content" in choice["delta"]:
                    content = choice["delta"]["content"]
                    if content:
                        full_content.append(content)

    return safe_json_dumps(chunks), "".join(full_content)


def _to_dict(value: Any) -> Any:
    """
    Recursively converts objects to dictionaries, focusing on essential data.
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {k: _to_dict(v) for k, v in value.items()}
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _to_dict(value.to_dict())
    if hasattr(value, "__dict__"):
        return _to_dict(vars(value))
    return str(value)
