import logging
from typing import Any, Dict, Iterator, Mapping, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    FiMimeTypeValues,
    FiSpanKindValues,
    MessageAttributes,
    SpanAttributes,
)
from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _TextGenerationRequestAttributesExtractor:
    """Extract span attributes from HuggingFace text_generation request."""

    def get_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from request."""
        yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "huggingface"
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "huggingface"

        model = request_parameters.get("model", "")
        if model:
            yield SpanAttributes.GEN_AI_REQUEST_MODEL, model

        yield SpanAttributes.INPUT_MIME_TYPE, FiMimeTypeValues.JSON.value
        yield SpanAttributes.OUTPUT_MIME_TYPE, FiMimeTypeValues.JSON.value

        # Invocation parameters
        invocation_params = self._extract_invocation_parameters(request_parameters)
        if invocation_params:
            yield SpanAttributes.GEN_AI_REQUEST_PARAMETERS, safe_json_dumps(invocation_params)

    def get_extra_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes including prompt."""
        # The prompt is the first positional argument
        prompt = request_parameters.get("prompt", "")
        if prompt:
            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "user"
            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", prompt
            yield SpanAttributes.INPUT_VALUE, prompt

        # System prompt if provided
        if "system_prompt" in request_parameters:
            yield SpanAttributes.GEN_AI_PROVIDER_NAME_PROMPT, request_parameters.get("system_prompt", "")

    def _extract_invocation_parameters(
        self, request_parameters: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Extract model invocation parameters."""
        params = {}
        param_keys = [
            "temperature", "max_new_tokens", "top_p", "top_k",
            "repetition_penalty", "stop_sequences", "seed",
            "do_sample", "return_full_text", "truncate",
            "typical_p", "watermark", "decoder_input_details",
        ]
        for key in param_keys:
            if key in request_parameters:
                params[key] = request_parameters[key]
        return params


class _ChatCompletionRequestAttributesExtractor:
    """Extract span attributes from HuggingFace chat_completion request."""

    def get_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from request."""
        yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "huggingface"
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "huggingface"

        model = request_parameters.get("model", "")
        if model:
            yield SpanAttributes.GEN_AI_REQUEST_MODEL, model

        yield SpanAttributes.INPUT_MIME_TYPE, FiMimeTypeValues.JSON.value
        yield SpanAttributes.OUTPUT_MIME_TYPE, FiMimeTypeValues.JSON.value

        # Invocation parameters
        invocation_params = self._extract_invocation_parameters(request_parameters)
        if invocation_params:
            yield SpanAttributes.GEN_AI_REQUEST_PARAMETERS, safe_json_dumps(invocation_params)

    def get_extra_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes including messages."""
        messages = request_parameters.get("messages", [])

        for i, msg in enumerate(messages):
            if isinstance(msg, dict):
                role = msg.get("role", "")
                content = msg.get("content", "")
            else:
                # Handle message objects
                role = getattr(msg, "role", "")
                content = getattr(msg, "content", "")

            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_ROLE}", role
            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_CONTENT}", str(content)

        # Set input value to the last user message
        if messages:
            last_msg = messages[-1]
            if isinstance(last_msg, dict):
                content = last_msg.get("content", "")
            else:
                content = getattr(last_msg, "content", "")
            yield SpanAttributes.INPUT_VALUE, str(content)

        # Tools
        if "tools" in request_parameters:
            tools = request_parameters.get("tools", [])
            yield "huggingface.tools_count", len(tools)
            yield "huggingface.tools", safe_json_dumps(tools)

        # Tool choice
        if "tool_choice" in request_parameters:
            yield "huggingface.tool_choice", str(request_parameters.get("tool_choice", ""))

    def _extract_invocation_parameters(
        self, request_parameters: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Extract model invocation parameters."""
        params = {}
        param_keys = [
            "temperature", "max_tokens", "top_p", "top_k",
            "repetition_penalty", "stop", "seed",
            "frequency_penalty", "presence_penalty",
            "stream", "n", "logprobs", "top_logprobs",
        ]
        for key in param_keys:
            if key in request_parameters:
                params[key] = request_parameters[key]
        return params


class _FeatureExtractionRequestAttributesExtractor:
    """Extract span attributes from HuggingFace feature_extraction (embeddings) request."""

    def get_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from request."""
        yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.EMBEDDING.value
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "huggingface"
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "huggingface"

        model = request_parameters.get("model", "")
        if model:
            yield SpanAttributes.EMBEDDING_MODEL_NAME, model

        yield SpanAttributes.INPUT_MIME_TYPE, FiMimeTypeValues.JSON.value
        yield SpanAttributes.OUTPUT_MIME_TYPE, FiMimeTypeValues.JSON.value

        # Normalize option
        if "normalize" in request_parameters:
            yield "huggingface.normalize", request_parameters.get("normalize")

        # Truncate option
        if "truncate" in request_parameters:
            yield "huggingface.truncate", request_parameters.get("truncate")

    def get_extra_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes."""
        text = request_parameters.get("text", "")
        if isinstance(text, list):
            yield SpanAttributes.INPUT_VALUE, safe_json_dumps(text[:5])  # Limit to 5
            yield "huggingface.texts_count", len(text)
        elif text:
            yield SpanAttributes.INPUT_VALUE, str(text)
            yield "huggingface.texts_count", 1
