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


class _ChatCompletionRequestAttributesExtractor:
    """Extract span attributes from DeepSeek chat completion request."""

    def get_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from request."""
        yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "deepseek"
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "deepseek"

        model = request_parameters.get("model", "deepseek-chat")
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

        # Track input messages
        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content", "")

            # Handle content that might be a list (for multimodal)
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                content = "\n".join(text_parts)

            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_ROLE}", role
            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_CONTENT}", str(content)

            # Handle tool calls in assistant messages
            if role == "assistant" and "tool_calls" in msg:
                tool_calls = msg.get("tool_calls", [])
                for j, tc in enumerate(tool_calls):
                    yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.tool_calls.{j}.id", tc.get("id", "")
                    yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.tool_calls.{j}.type", tc.get("type", "function")
                    if "function" in tc:
                        yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.tool_calls.{j}.function.name", tc["function"].get("name", "")
                        yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.tool_calls.{j}.function.arguments", tc["function"].get("arguments", "")

        # System prompt extraction
        for msg in messages:
            if msg.get("role") == "system":
                yield SpanAttributes.GEN_AI_PROVIDER_NAME_PROMPT, str(msg.get("content", ""))
                break

        # User input for display
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                    content = "\n".join(text_parts)
                yield SpanAttributes.INPUT_VALUE, str(content)
                break

        # Tools
        if "tools" in request_parameters:
            tools = request_parameters.get("tools", [])
            yield "deepseek.tools_count", len(tools)
            for i, tool in enumerate(tools[:10]):  # Limit to first 10
                if "function" in tool:
                    func = tool["function"]
                    yield f"deepseek.tools.{i}.name", func.get("name", "")
                    yield f"deepseek.tools.{i}.description", func.get("description", "")[:200]  # Truncate

        # Response format
        if "response_format" in request_parameters:
            resp_format = request_parameters.get("response_format", {})
            if isinstance(resp_format, dict):
                yield "deepseek.response_format", resp_format.get("type", "text")

    def _extract_invocation_parameters(
        self, request_parameters: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Extract model invocation parameters."""
        params = {}
        param_keys = [
            "temperature", "max_tokens", "top_p", "stop", "stream",
            "frequency_penalty", "presence_penalty", "seed", "logprobs",
            "top_logprobs", "n"
        ]
        for key in param_keys:
            if key in request_parameters:
                params[key] = request_parameters[key]
        return params
