import logging
from typing import Any, Dict, Iterator, List, Mapping, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    FiMimeTypeValues,
    FiSpanKindValues,
    MessageAttributes,
    SpanAttributes,
    ToolCallAttributes,
)
from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _ChatCompletionsRequestAttributesExtractor:
    """Extract span attributes from Together AI chat completions request."""

    def get_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from request."""
        yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "together"
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "together"

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
        if messages:
            yield SpanAttributes.INPUT_VALUE, safe_json_dumps(messages)
            for i, msg in enumerate(messages):
                if isinstance(msg, dict):
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_ROLE}", role
                    if isinstance(content, str):
                        yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_CONTENT}", content
                    elif isinstance(content, list):
                        # Handle multi-modal content
                        yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_CONTENT}", safe_json_dumps(content)

                    # Handle tool calls in message
                    if "tool_calls" in msg and msg["tool_calls"]:
                        for j, tc in enumerate(msg["tool_calls"]):
                            if isinstance(tc, dict):
                                if "id" in tc:
                                    yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{j}.{ToolCallAttributes.TOOL_CALL_ID}", tc["id"]
                                if "function" in tc:
                                    func = tc["function"]
                                    if "name" in func:
                                        yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{j}.{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}", func["name"]
                                    if "arguments" in func:
                                        yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{j}.{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}", func["arguments"]

        # Tools
        if "tools" in request_parameters:
            tools = request_parameters.get("tools", [])
            if tools:
                for i, tool in enumerate(tools):
                    yield f"{SpanAttributes.GEN_AI_TOOL_DEFINITIONS}.{i}.tool.json_schema", safe_json_dumps(tool)

    def _extract_invocation_parameters(
        self, request_parameters: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Extract model invocation parameters."""
        params = {}
        param_keys = [
            "temperature", "max_tokens", "top_p", "top_k", "stop",
            "frequency_penalty", "presence_penalty", "repetition_penalty",
            "logprobs", "echo", "n", "stream", "response_format"
        ]
        for key in param_keys:
            if key in request_parameters:
                params[key] = request_parameters[key]
        return params


class _CompletionsRequestAttributesExtractor:
    """Extract span attributes from Together AI completions request."""

    def get_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from request."""
        yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "together"
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "together"

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
        prompt = request_parameters.get("prompt", "")
        if prompt:
            if isinstance(prompt, str):
                yield SpanAttributes.INPUT_VALUE, prompt
                yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "user"
                yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", prompt
            elif isinstance(prompt, list):
                yield SpanAttributes.INPUT_VALUE, safe_json_dumps(prompt)
                for i, p in enumerate(prompt):
                    yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_ROLE}", "user"
                    yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_CONTENT}", str(p)

    def _extract_invocation_parameters(
        self, request_parameters: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Extract model invocation parameters."""
        params = {}
        param_keys = [
            "temperature", "max_tokens", "top_p", "top_k", "stop",
            "frequency_penalty", "presence_penalty", "repetition_penalty",
            "logprobs", "echo", "n", "stream", "suffix"
        ]
        for key in param_keys:
            if key in request_parameters:
                params[key] = request_parameters[key]
        return params


class _EmbeddingsRequestAttributesExtractor:
    """Extract span attributes from Together AI embeddings request."""

    def get_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from request."""
        yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.EMBEDDING.value
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "together"
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "together"

        model = request_parameters.get("model", "")
        if model:
            yield SpanAttributes.EMBEDDING_MODEL_NAME, model

        yield SpanAttributes.INPUT_MIME_TYPE, FiMimeTypeValues.JSON.value
        yield SpanAttributes.OUTPUT_MIME_TYPE, FiMimeTypeValues.JSON.value

    def get_extra_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes."""
        input_text = request_parameters.get("input", "")
        if input_text:
            if isinstance(input_text, str):
                yield SpanAttributes.INPUT_VALUE, input_text
                yield "together.texts_count", 1
            elif isinstance(input_text, list):
                yield SpanAttributes.INPUT_VALUE, safe_json_dumps(input_text[:5])  # Limit for display
                yield "together.texts_count", len(input_text)
