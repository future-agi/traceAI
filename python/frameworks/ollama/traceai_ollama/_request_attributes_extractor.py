import logging
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from fi_instrumentation import TraceConfig, safe_json_dumps
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
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

    def __init__(self, config: Optional[TraceConfig] = None) -> None:
        self._config = config if config is not None else TraceConfig()

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
        system_messages = []
        if "system" in request_parameters:
            system_messages.append({"role": "system", "content": request_parameters["system"]})

        # For chat endpoint
        if "messages" in request_parameters:
            messages = system_messages + list(request_parameters.get("messages", []))
            yield from self._extract_messages(messages)
            if self._config.hide_input_messages:
                yield SpanAttributes.INPUT_VALUE, REDACTED_VALUE
            else:
                # Serialize a detached telemetry copy; the SDK still receives its
                # original messages and image payloads without modification.
                serialized = []
                for message in messages:
                    item = dict(message)
                    if self._config.hide_input_text and "content" in item:
                        item["content"] = REDACTED_VALUE
                    if self._config.hide_input_images:
                        item.pop("images", None)
                    serialized.append(item)
                yield SpanAttributes.INPUT_VALUE, safe_json_dumps({"messages": serialized})

        # For generate endpoint
        elif "prompt" in request_parameters:
            prompt = request_parameters.get("prompt", "")
            yield from self._extract_messages(system_messages + [{"role": "user", "content": prompt}])
            yield SpanAttributes.INPUT_VALUE, REDACTED_VALUE if (
                self._config.hide_input_text or self._config.hide_input_messages
            ) else prompt

        # For embed endpoint
        elif "input" in request_parameters:
            input_text = request_parameters.get("input", "")
            if self._config.hide_input_text or self._config.hide_input_messages:
                yield SpanAttributes.INPUT_VALUE, REDACTED_VALUE
            elif isinstance(input_text, list):
                yield SpanAttributes.INPUT_VALUE, safe_json_dumps(input_text)
            else:
                yield SpanAttributes.INPUT_VALUE, str(input_text)

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
            if "images" in message and not self._config.hide_input_images:
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
