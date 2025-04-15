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
from fi_instrumentation.fi_types import FiMimeTypeValues, SpanAttributes
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

    if eval_input and len(eval_input) > 0:
        yield SpanAttributes.QUERY, eval_input[0]

    eval_input = "\n".join(eval_input)
    yield SpanAttributes.EVAL_INPUT, eval_input


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

    yield SpanAttributes.RAW_OUTPUT, safe_json_dumps(raw_output_dict)


def _as_streaming_output(chunks: List[ChatCompletionChunk]):
    processed_chunks = [chunk.to_dict() for chunk in chunks]
    output_value = ""
    for chunk in processed_chunks:
        if "choices" in chunk and chunk["choices"]:
            delta = chunk["choices"][0].get("delta", {})
            if "content" in delta:
                output_value += delta["content"]

    # Only check the last chunk for token information
    if processed_chunks:
        last_chunk = processed_chunks[-1]
        x_groq = last_chunk.get("x_groq", {})
        model = last_chunk.get("model", "")
        if x_groq:
            usage = x_groq.get("usage", {})
            if usage:
                total_tokens = usage.get("total_tokens")
                if total_tokens is not None:
                    yield SpanAttributes.LLM_TOKEN_COUNT_TOTAL, total_tokens

                prompt_tokens = usage.get("prompt_tokens")
                if prompt_tokens is not None:
                    yield SpanAttributes.LLM_TOKEN_COUNT_PROMPT, prompt_tokens

                completion_tokens = usage.get("completion_tokens")
                if completion_tokens is not None:
                    yield SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, completion_tokens

        if model:
            yield SpanAttributes.LLM_MODEL_NAME, model

    yield SpanAttributes.OUTPUT_VALUE, output_value
    yield SpanAttributes.RAW_OUTPUT, safe_json_dumps(processed_chunks)


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
