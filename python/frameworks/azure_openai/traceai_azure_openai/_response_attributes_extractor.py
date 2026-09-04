from __future__ import annotations

import base64
import logging
from importlib import import_module

from types import ModuleType
from typing import (
    TYPE_CHECKING,
    Any,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
)

from fi_instrumentation.fi_types import (
    EmbeddingAttributes,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
    ToolCallAttributes,
)
from opentelemetry.util.types import AttributeValue
from traceai_azure_openai._attributes._responses_api import _ResponsesApiAttributes
from traceai_azure_openai._utils import _get_openai_version, _get_texts

if TYPE_CHECKING:
    from openai.types import Completion, CreateEmbeddingResponse
    from openai.types.chat import ChatCompletion
    from openai.types.images_response import ImagesResponse
    from openai.types.responses.response import Response

__all__ = ("_ResponseAttributesExtractor",)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


try:
    _NUMPY: Optional[ModuleType] = import_module("numpy")
except ImportError:
    _NUMPY = None


class _ResponseAttributesExtractor:
    __slots__ = (
        "_openai",
        "_chat_completion_type",
        "_completion_type",
        "_create_embedding_response_type",
        "_images_response_type",
        "_responses_type",
    )

    def __init__(self, openai: ModuleType) -> None:
        self._openai = openai
        self._chat_completion_type: Type["ChatCompletion"] = (
            openai.types.chat.ChatCompletion
        )
        self._completion_type: Type["Completion"] = openai.types.Completion
        self._responses_type: Type["Response"] = openai.types.responses.response.Response
        self._create_embedding_response_type: Type["CreateEmbeddingResponse"] = (
            openai.types.CreateEmbeddingResponse
        )
        self._images_response_type: Type["ImagesResponse"] = (
            openai.types.images_response.ImagesResponse
        )

    def get_attributes_from_response(
        self,
        response: Any,
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if isinstance(response, self._chat_completion_type):
            yield from self._get_attributes_from_chat_completion(
                completion=response,
                request_parameters=request_parameters,
            )
        elif isinstance(response, self._responses_type):
            yield from self._get_attributes_from_responses_response(
                response=response,
                request_parameters=request_parameters,
            )
        elif isinstance(response, self._create_embedding_response_type):
            yield from self._get_attributes_from_create_embedding_response(
                response=response,
                request_parameters=request_parameters,
            )
        elif isinstance(response, self._completion_type):
            yield from self._get_attributes_from_completion(
                completion=response,
                request_parameters=request_parameters,
            )
        elif isinstance(response, self._images_response_type):
            yield from self._get_attributes_from_image_generation(
                data=response.data,
                request_parameters=request_parameters,
            )
        else:
            yield from ()

    def _get_attributes_from_responses_response(
        self,
        response: Response,
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:
        yield from _ResponsesApiAttributes._get_attributes_from_response(response)

    def _get_attributes_from_chat_completion(
        self,
        completion: "ChatCompletion",
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if model := getattr(completion, "model", None):
            yield SpanAttributes.GEN_AI_REQUEST_MODEL, model
        if usage := getattr(completion, "usage", None):
            yield from self._get_attributes_from_completion_usage(usage)

        if (choices := getattr(completion, "choices", None)) and isinstance(
            choices, Iterable
        ):
            for choice in choices:
                if (index := getattr(choice, "index", None)) is None:
                    continue
                if message := getattr(choice, "message", None):
                    for key, value in self._get_attributes_from_chat_completion_message(
                        message
                    ):
                        yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{key}", value

    def _get_attributes_from_completion(
        self,
        completion: "Completion",
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if model := getattr(completion, "model", None):
            yield SpanAttributes.GEN_AI_REQUEST_MODEL, model
        if usage := getattr(completion, "usage", None):
            yield from self._get_attributes_from_completion_usage(usage)
        if model_prompt := request_parameters.get("prompt"):
            if prompts := list(_get_texts(model_prompt, model)):
                yield SpanAttributes.GEN_AI_PROMPTS, prompts

    def _get_attributes_from_create_embedding_response(
        self,
        response: "CreateEmbeddingResponse",
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if usage := getattr(response, "usage", None):
            yield from self._get_attributes_from_embedding_usage(usage)
        if model := getattr(response, "model"):
            yield f"{SpanAttributes.EMBEDDING_MODEL_NAME}", model
        if (data := getattr(response, "data", None)) and isinstance(data, Iterable):
            for embedding in data:
                if (index := getattr(embedding, "index", None)) is None:
                    continue
                for key, value in self._get_attributes_from_embedding(embedding):
                    yield f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.{index}.{key}", value

        embedding_input = request_parameters.get("input")
        for index, text in enumerate(_get_texts(embedding_input, model)):
            yield (
                (
                    f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.{index}."
                    f"{EmbeddingAttributes.EMBEDDING_TEXT}"
                ),
                text,
            )

    def _get_attributes_from_embedding(
        self,
        embedding: object,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if not (_vector := getattr(embedding, "embedding", None)):
            return
        if (
            isinstance(_vector, Sequence)
            and len(_vector)
            and isinstance(_vector[0], float)
        ):
            vector = list(_vector)
            yield f"{EmbeddingAttributes.EMBEDDING_VECTOR}", vector
        elif isinstance(_vector, str) and _vector and _NUMPY:
            try:
                vector = _NUMPY.frombuffer(
                    base64.b64decode(_vector), dtype="float32"
                ).tolist()
            except Exception:
                logger.exception("Failed to decode embedding")
                pass
            else:
                yield f"{EmbeddingAttributes.EMBEDDING_VECTOR}", vector

    def _get_attributes_from_chat_completion_message(
        self,
        message: object,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if role := getattr(message, "role", None):
            yield MessageAttributes.MESSAGE_ROLE, role
        if content := getattr(message, "content", None):
            yield MessageAttributes.MESSAGE_CONTENT, content
        if audio := getattr(message, "audio", None):
            # Handle audio attributes
            yield f"{MessageAttributes.MESSAGE_CONTENT}.0.{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "audio"
            if audio_data := getattr(audio, "data", None):
                yield f"{MessageAttributes.MESSAGE_CONTENT}.0.{MessageContentAttributes.MESSAGE_CONTENT_AUDIO}", audio_data
            if transcript := getattr(audio, "transcript", None):
                yield f"{MessageAttributes.MESSAGE_CONTENT}.0.{MessageContentAttributes.MESSAGE_AUDIO_TRANSCRIPT}", transcript
        if function_call := getattr(message, "function_call", None):
            if name := getattr(function_call, "name", None):
                yield MessageAttributes.MESSAGE_FUNCTION_CALL_NAME, name
            if arguments := getattr(function_call, "arguments", None):
                yield MessageAttributes.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON, arguments
        if (
            _get_openai_version() >= (1, 1, 0)
            and (tool_calls := getattr(message, "tool_calls", None))
            and isinstance(tool_calls, Iterable)
        ):
            for index, tool_call in enumerate(tool_calls):
                if (tool_call_id := getattr(tool_call, "id", None)) is not None:
                    yield (
                        f"{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}."
                        f"{ToolCallAttributes.TOOL_CALL_ID}",
                        tool_call_id,
                    )
                if function := getattr(tool_call, "function", None):
                    if name := getattr(function, "name", None):
                        yield (
                            (
                                f"{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}."
                                f"{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}"
                            ),
                            name,
                        )
                    if arguments := getattr(function, "arguments", None):
                        yield (
                            f"{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}."
                            f"{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
                            arguments,
                        )

    def _get_attributes_from_completion_usage(
        self,
        usage: object,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if (total_tokens := getattr(usage, "total_tokens", None)) is not None:
            yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, total_tokens
        if (prompt_tokens := getattr(usage, "prompt_tokens", None)) is not None:
            yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, prompt_tokens
        if (completion_tokens := getattr(usage, "completion_tokens", None)) is not None:
            yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, completion_tokens

    def _get_attributes_from_embedding_usage(
        self,
        usage: object,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if (total_tokens := getattr(usage, "total_tokens", None)) is not None:
            yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, total_tokens
        if (prompt_tokens := getattr(usage, "prompt_tokens", None)) is not None:
            yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, prompt_tokens

    def _get_attributes_from_image_generation(
        self,
        data: Iterable[object],
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:

        if model := request_parameters.get("model"):
            yield SpanAttributes.GEN_AI_REQUEST_MODEL, model

        for index, obj in enumerate(data):
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{MessageAttributes.MESSAGE_ROLE}", "assistant"
            if image := getattr(obj, "url", None):
                yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{MessageAttributes.MESSAGE_CONTENT}.0.{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "image"
                yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{MessageAttributes.MESSAGE_CONTENT}.0.{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}", image
            elif b64_json := getattr(obj, "b64_json", None):
                yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{MessageAttributes.MESSAGE_CONTENT}.0.{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "image"
                yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{MessageAttributes.MESSAGE_CONTENT}.0.{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}", b64_json
