import logging
from typing import Any, Dict, Iterator, List, Mapping, Tuple

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


class _RequestAttributesExtractor:
    """Extract span attributes from Ollama request parameters."""

    def get_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from request."""
        # Determine span kind based on endpoint
        model = request_parameters.get("model", "")

        # Check if this is an embedding request
        if "input" in request_parameters or request_parameters.get("_endpoint") == "embed":
            yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.EMBEDDING.value
            yield SpanAttributes.EMBEDDING_MODEL_NAME, model
        else:
            yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value
            yield SpanAttributes.GEN_AI_REQUEST_MODEL, model

        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "ollama"
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "ollama"

        # Input/Output mime types
        yield SpanAttributes.INPUT_MIME_TYPE, FiMimeTypeValues.JSON.value
        yield SpanAttributes.OUTPUT_MIME_TYPE, FiMimeTypeValues.JSON.value

        # Extract invocation parameters
        invocation_params = self._extract_invocation_parameters(request_parameters)
        if invocation_params:
            yield SpanAttributes.GEN_AI_REQUEST_PARAMETERS, safe_json_dumps(invocation_params)

    def get_extra_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes including messages."""
        # For chat endpoint
        if "messages" in request_parameters:
            messages = request_parameters.get("messages", [])
            yield from self._extract_messages(messages)
            yield SpanAttributes.INPUT_VALUE, safe_json_dumps({"messages": messages})

        # For generate endpoint
        elif "prompt" in request_parameters:
            prompt = request_parameters.get("prompt", "")
            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "user"
            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", prompt
            yield SpanAttributes.INPUT_VALUE, prompt

        # For embed endpoint
        elif "input" in request_parameters:
            input_text = request_parameters.get("input", "")
            if isinstance(input_text, list):
                yield SpanAttributes.INPUT_VALUE, safe_json_dumps(input_text)
            else:
                yield SpanAttributes.INPUT_VALUE, str(input_text)

        # System prompt if provided
        if "system" in request_parameters:
            system = request_parameters.get("system", "")
            yield SpanAttributes.GEN_AI_PROVIDER_NAME_PROMPT, system

    def _extract_messages(
        self, messages: List[Dict[str, Any]]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract message attributes."""
        for i, message in enumerate(messages):
            role = message.get("role", "")
            content = message.get("content", "")

            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_ROLE}", role
            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_CONTENT}", content

            # Handle images in multimodal messages
            if "images" in message:
                images = message.get("images", [])
                yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.images", safe_json_dumps(images[:3])  # Limit to 3

    def _extract_invocation_parameters(
        self, request_parameters: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Extract model invocation parameters."""
        params = {}

        # Common parameters
        param_keys = [
            "temperature", "top_p", "top_k", "num_predict", "num_ctx",
            "stop", "repeat_penalty", "presence_penalty", "frequency_penalty",
            "seed", "num_gpu", "num_thread", "format"
        ]

        for key in param_keys:
            if key in request_parameters:
                params[key] = request_parameters[key]

        # Options dict (Ollama-specific)
        if "options" in request_parameters:
            params["options"] = request_parameters["options"]

        return params
