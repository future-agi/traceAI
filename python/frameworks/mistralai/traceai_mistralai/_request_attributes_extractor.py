import logging
from enum import Enum
from types import ModuleType
from typing import Any, Iterable, Iterator, List, Mapping, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
    ToolCallAttributes,
)
from opentelemetry.util.types import AttributeValue

__all__ = ("_RequestAttributesExtractor",)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _RequestAttributesExtractor:
    __slots__ = (
        "_mistralai",
        "_chat_completion_type",
        "_completion_type",
        "_create_embedding_response_type",
    )

    def __init__(self, mistralai: ModuleType) -> None:
        self._mistralai = mistralai

    def get_attributes_from_request(
        self,
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if not isinstance(request_parameters, Mapping):
            return
        yield from _get_attributes_from_chat_completion_create_param(request_parameters)


def _get_attributes_from_chat_completion_create_param(
    params: Mapping[str, Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    if not isinstance(params, Mapping):
        return
    invocation_params = dict(params)
    invocation_params.pop("messages", None)
    invocation_params.pop("functions", None)
    invocation_params.pop("tools", None)
    yield SpanAttributes.LLM_INVOCATION_PARAMETERS, safe_json_dumps(invocation_params)
    if (input_messages := params.get("messages")) and isinstance(
        input_messages, Iterable
    ):
        # Use reversed() to get the last message first. This is because OTEL has a default limit of
        # 128 attributes per span, and flattening increases the number of attributes very quickly.
        for index, input_message in reversed(list(enumerate(input_messages))):
            for key, value in _get_attributes_from_message_param(input_message):
                yield f"{SpanAttributes.LLM_INPUT_MESSAGES}.{index}.{key}", value


def _get_attributes_from_message_param(
    message: Mapping[str, Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    if not hasattr(message, "get"):
        return
    if role := message.get("role"):
        yield (
            MessageAttributes.MESSAGE_ROLE,
            role.value if isinstance(role, Enum) else role,
        )
    if content := message.get("content"):
        if isinstance(content, str):
            yield MessageAttributes.MESSAGE_CONTENT, content
        elif isinstance(content, List):
            for index, c in enumerate(content):
                for key, value in _get_attributes_from_message_content(c):
                    yield f"{MessageAttributes.MESSAGE_CONTENTS}.{index}.{key}", value

    if name := message.get("name"):
        yield MessageAttributes.MESSAGE_NAME, name
    if (function_call := message.get("function_call")) and hasattr(
        function_call, "get"
    ):
        if function_name := function_call.get("name"):
            yield MessageAttributes.MESSAGE_FUNCTION_CALL_NAME, function_name
        if function_arguments := function_call.get("arguments"):
            yield (
                MessageAttributes.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON,
                function_arguments,
            )
    if (tool_calls := message.get("tool_calls"),) and isinstance(tool_calls, Iterable):
        for index, tool_call in enumerate(tool_calls):
            if not hasattr(tool_call, "get"):
                continue
            if (function := tool_call.get("function")) and hasattr(function, "get"):
                if name := function.get("name"):
                    yield (
                        f"{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}."
                        f"{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}",
                        name,
                    )
                if arguments := function.get("arguments"):
                    yield (
                        f"{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}."
                        f"{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
                        arguments,
                    )


def _get_attributes_from_message_content(
    content: Mapping[str, Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    if not hasattr(content, "get"):
        return
    content_dict = dict(content)
    type_ = content_dict.pop("type", None)
    if type_ == "text":
        if text := content_dict.get("text"):
            yield f"{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "text"
            yield f"{MessageContentAttributes.MESSAGE_CONTENT_TEXT}", text
    elif type_ == "image_url":
        if image := content_dict.get("image_url"):
            yield f"{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "image"
            yield f"{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}", image
