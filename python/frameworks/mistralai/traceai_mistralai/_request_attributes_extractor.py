import logging
from enum import Enum
from types import ModuleType
from typing import Any, Iterable, Iterator, List, Mapping, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    FiLLMProviderValues,
    ImageAttributes,
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
        yield from _get_model_attributes(request_parameters)
        yield from _get_attributes_from_chat_completion_create_param(request_parameters)


def _get_model_attributes(
    params: Mapping[str, Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    # Attribution must survive HTTP errors and hidden invocation parameters.
    yield SpanAttributes.GEN_AI_PROVIDER_NAME, FiLLMProviderValues.MISTRALAI.value
    if isinstance(model := params.get("model"), str) and model:
        yield SpanAttributes.GEN_AI_REQUEST_MODEL, model


def _get_attributes_from_chat_completion_create_param(
    params: Mapping[str, Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    if not isinstance(params, Mapping):
        return
    invocation_params = dict(params)
    invocation_params.pop("messages", None)
    invocation_params.pop("functions", None)
    invocation_params.pop("tools", None)
    yield SpanAttributes.GEN_AI_REQUEST_PARAMETERS, safe_json_dumps(invocation_params)
    if (input_messages := params.get("messages")) and isinstance(
        input_messages, Iterable
    ):
        # Use reversed() to get the last message first. This is because OTEL has a default limit of
        # 128 attributes per span, and flattening increases the number of attributes very quickly.
        for index, input_message in reversed(list(enumerate(input_messages))):
            for key, value in _get_attributes_from_message_param(input_message):
                yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{index}.{key}", value


def _get_attributes_from_message_param(
    message: Mapping[str, Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    message = _message_dict(message)
    if not message:
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
    if tool_call_id := message.get("tool_call_id"):
        yield MessageAttributes.MESSAGE_TOOL_CALL_ID, tool_call_id
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
    if (tool_calls := message.get("tool_calls")) and isinstance(tool_calls, Iterable):
        for index, tool_call in enumerate(tool_calls):
            tool_call = _message_dict(tool_call)
            if tool_call_id := tool_call.get("id"):
                yield (
                    f"{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}."
                    f"{ToolCallAttributes.TOOL_CALL_ID}",
                    tool_call_id,
                )
            function = _message_dict(tool_call.get("function")) if tool_call else None
            if function:
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
    content_dict = dict(_message_dict(content))
    if not content_dict:
        return
    type_ = content_dict.pop("type", None)
    if type_ is None and type(content).__module__.startswith("mistralai."):
        # SDK 1.1 omits default discriminators on typed content models.
        type_ = {"TextChunk": "text", "ImageURLChunk": "image_url"}.get(
            type(content).__name__
        )
    if type_ == "text":
        if text := content_dict.get("text"):
            yield f"{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "text"
            yield f"{MessageContentAttributes.MESSAGE_CONTENT_TEXT}", text
    elif type_ == "image_url":
        if image := content_dict.get("image_url"):
            # The core applies the base64 limit only to semantic image.url leaves.
            url = image if isinstance(image, str) else _message_dict(image).get("url")
            if isinstance(url, str):
                yield MessageContentAttributes.MESSAGE_CONTENT_TYPE, "image"
                yield f"{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}.{ImageAttributes.IMAGE_URL}", url


def _message_dict(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    # SDK 1.x exposes Pydantic message and content models. Normalize only
    # their known fields, never copy raw models into non-policy attributes.
    if callable(getattr(value, "model_dump", None)):
        fields = value.model_dump(exclude_none=True)
        if hasattr(value, "content"):
            # Preserve typed nested chunks: SDK 1.1 drops their discriminator
            # during model_dump even though it sends them to the model.
            fields["content"] = value.content
        return fields
    return {}
