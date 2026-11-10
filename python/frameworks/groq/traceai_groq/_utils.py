import logging
import warnings
from typing import (
    Any,
    Iterable,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    FiMimeTypeValues, MessageAttributes, SpanAttributes, ToolCallAttributes,
)
from groq.types.chat import ChatCompletionChunk
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue
from traceai_groq._with_span import _WithSpan

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
    attributes_dict = {}
    extra_attributes_dict = {}
    try:
        for key, value in attributes:
            attributes_dict[key] = value
    except Exception:
        logger.exception("Failed to get attributes")
    try:
        for key, value in extra_attributes:
            extra_attributes_dict[key] = value
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


def _extract_eval_input(
    messages: Iterable[Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    eval_input = []
    for message in messages:
        if (
            isinstance(message, dict)
            and "content" in message
            and isinstance(message["content"], str)
        ):
            eval_input.append(message["content"])

    eval_input = "\n".join(eval_input)
    yield SpanAttributes.INPUT_VALUE, eval_input


def _process_response(response: Any) -> str:
    """
    Extract the content from a Groq chat completion response.
    """
    try:
        # Handle dictionary response
        if isinstance(response, dict):
            choices = response.get("choices", [])
            if choices and isinstance(choices, (list, tuple)):
                first_choice = choices[0]
                if isinstance(first_choice, dict):
                    message = first_choice.get("message", {})
                    if isinstance(message, dict):
                        content = message.get("content", "")
                        return str(content) if content is not None else ""

        # Handle object-style response
        if hasattr(response, "choices"):
            choices = response.choices
            if choices and len(choices) > 0:
                if hasattr(choices[0], "message"):
                    message = choices[0].message
                    if hasattr(message, "content"):
                        content = message.content
                        return str(content) if content is not None else ""

        return response

    except Exception:
        return ""


def _as_raw_output(raw_output: Any) -> Iterator[Tuple[str, AttributeValue]]:
    if hasattr(raw_output, "model_dump"):
        raw_output_dict = raw_output.model_dump()
    elif hasattr(raw_output, "to_dict") and callable(raw_output.to_dict):
        raw_output_dict = raw_output.to_dict()
    elif hasattr(raw_output, "__dict__"):
        raw_output_dict = raw_output.__dict__
    else:
        raw_output_dict = raw_output

    yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(raw_output_dict)


def _as_streaming_output(chunks: List[ChatCompletionChunk]):
    """Assemble SDK deltas by choice/tool index without exporting raw payloads."""
    messages = {}
    usage = {}
    model = None
    for item in chunks:
        chunk = _to_dict(item)
        if not isinstance(chunk, dict):
            continue
        model = chunk.get("model") or model
        # SDK versions expose usage either directly or in Groq's extension.
        chunk_usage = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage")
        if chunk_usage:
            usage.update({key: value for key, value in chunk_usage.items() if value is not None})
        for choice in chunk.get("choices") or []:
            index = choice.get("index", 0)
            message = messages.setdefault(index, {"role": "assistant", "content": "", "tools": {}})
            delta = choice.get("delta") or {}
            message["role"] = delta.get("role") or message["role"]
            # Role-only, tool-only and terminal chunks commonly have null content.
            if isinstance(delta.get("content"), str):
                message["content"] += delta["content"]
            for tool_delta in delta.get("tool_calls") or []:
                tool = message["tools"].setdefault(
                    tool_delta.get("index", 0), {"id": "", "name": "", "arguments": ""}
                )
                if tool_delta.get("id"):
                    tool["id"] += tool_delta["id"]
                function = tool_delta.get("function") or {}
                for field in ("name", "arguments"):
                    if isinstance(function.get(field), str):
                        tool[field] += function[field]

    if model:
        yield SpanAttributes.GEN_AI_RESPONSE_MODEL, model
    for field, attribute in (
        ("total_tokens", SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS),
        ("prompt_tokens", SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS),
        ("completion_tokens", SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS),
    ):
        if usage.get(field) is not None:
            yield attribute, usage[field]

    for index, message in sorted(messages.items()):
        prefix = f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}"
        yield f"{prefix}.{MessageAttributes.MESSAGE_ROLE}", message["role"]
        if message["content"]:
            yield f"{prefix}.{MessageAttributes.MESSAGE_CONTENT}", message["content"]
        for tool_index, tool in sorted(message["tools"].items()):
            tool_prefix = f"{prefix}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{tool_index}"
            for field, attribute in (
                ("id", ToolCallAttributes.TOOL_CALL_ID),
                ("name", ToolCallAttributes.TOOL_CALL_FUNCTION_NAME),
                ("arguments", ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON),
            ):
                if tool[field]:
                    yield f"{tool_prefix}.{attribute}", tool[field]

    # Preserve the text contract; tool payloads live in the privacy-controlled
    # output-message attributes above, including tool-only completions.
    yield SpanAttributes.OUTPUT_VALUE, messages.get(0, {}).get("content", "")


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
