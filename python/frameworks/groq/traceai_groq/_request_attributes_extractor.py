import logging
from enum import Enum
from typing import Any, Iterable, Iterator, Mapping, Tuple, TypeVar

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    FiSpanKindValues,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
    ToolCallAttributes,
    ToolAttributes,
)
from opentelemetry.util.types import AttributeValue
from traceai_groq._utils import (
    _as_input_attributes,
    _extract_eval_input,
    _io_value_and_type,
)

__all__ = ("_RequestAttributesExtractor",)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _RequestAttributesExtractor:
    __slots__ = ()

    def get_attributes_from_request(
        self,
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:
        yield SpanAttributes.FI_SPAN_KIND, FiSpanKindValues.LLM.value
        try:
            yield SpanAttributes.RAW_INPUT, safe_json_dumps(request_parameters)

            messages = request_parameters.get("messages")
            input_data = messages if messages is not None else request_parameters

            if messages and isinstance(messages, Iterable):
                yield from _extract_eval_input(messages)

            yield from _as_input_attributes(
                _io_value_and_type(input_data),
            )
        except Exception as e:
            logger.exception(
                f"Failed to get input attributes from request parameters of "
                f"type {type(request_parameters)}"
            )
            raise

    def get_extra_attributes_from_request(
        self,
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if not isinstance(request_parameters, Mapping):
            return
        invocation_params = dict(request_parameters)
        invocation_params.pop("messages", None)  # Remove LLM input messages
        invocation_params.pop("functions", None)

        if isinstance((tools := invocation_params.pop("tools", None)), Iterable):
            for i, tool in enumerate(tools):
                yield f"{SpanAttributes.LLM_TOOLS}.{i}.{ToolAttributes.TOOL_JSON_SCHEMA}", safe_json_dumps(
                    tool
                )

        yield SpanAttributes.LLM_INVOCATION_PARAMETERS, safe_json_dumps(
            invocation_params
        )

        if (input_messages := request_parameters.get("messages")) and isinstance(
            input_messages, Iterable
        ):
            for index, input_message in list(enumerate(input_messages)):
                for key, value in self._get_attributes_from_message_param(
                    input_message
                ):
                    yield f"{SpanAttributes.LLM_INPUT_MESSAGES}.{index}.{key}", value

    def _get_attributes_from_message_param(
        self,
        message: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if role := get_attribute(message, "role"):
            yield (
                MessageAttributes.MESSAGE_ROLE,
                role.value if isinstance(role, Enum) else role,
            )
        if content := get_attribute(message, "content"):
            if isinstance(content, str):
                yield (
                    MessageAttributes.MESSAGE_CONTENT,
                    content,
                )
            elif isinstance(content, Iterable):
                for index, c in list(enumerate(content)):
                    for key, value in self._get_attributes_from_message_content(c):
                        yield f"{MessageAttributes.MESSAGE_CONTENTS}.{index}.{key}", value

        if name := get_attribute(message, "name"):
            yield MessageAttributes.MESSAGE_NAME, name

        if tool_call_id := get_attribute(message, "tool_call_id"):
            yield MessageAttributes.MESSAGE_TOOL_CALL_ID, tool_call_id

        # Deprecated by Groq
        if function_call := get_attribute(message, "function_call"):
            if function_name := get_attribute(function_call, "name"):
                yield MessageAttributes.MESSAGE_FUNCTION_CALL_NAME, function_name
            if function_arguments := get_attribute(function_call, "arguments"):
                yield (
                    MessageAttributes.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON,
                    function_arguments,
                )

        if (tool_calls := get_attribute(message, "tool_calls")) and isinstance(
            tool_calls, Iterable
        ):
            for index, tool_call in enumerate(tool_calls):
                if (tool_call_id := get_attribute(tool_call, "id")) is not None:
                    yield (
                        f"{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}."
                        f"{ToolCallAttributes.TOOL_CALL_ID}",
                        tool_call_id,
                    )
                if function := get_attribute(tool_call, "function"):
                    if name := get_attribute(function, "name"):
                        yield (
                            f"{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}."
                            f"{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}",
                            name,
                        )
                    if arguments := get_attribute(function, "arguments"):
                        yield (
                            f"{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}."
                            f"{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
                            arguments,
                        )

    def _get_attributes_from_message_content(
        self,
        content: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:

        content = dict(content)
        type_ = content.pop("type", None)
        if type_ is None:
            return
        
        if type_ == "text":
            if text := content.pop("text"):
                yield f"{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "text"
                yield f"{MessageContentAttributes.MESSAGE_CONTENT_TEXT}", text
        elif type_ == "image_url":
            if image := content.pop("image_url"):
                yield f"{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "image"
                yield f"{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}", image.get(
                    "url", ""
                )


T = TypeVar("T", bound=type)


def is_iterable_of(lst: Iterable[object], tp: T) -> bool:
    return isinstance(lst, Iterable) and all(isinstance(x, tp) for x in lst)


def get_attribute(obj: Any, attr_name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(attr_name, default)
    return getattr(obj, attr_name, default)
