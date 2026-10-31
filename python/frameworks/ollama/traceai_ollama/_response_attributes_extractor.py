import logging
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from fi_instrumentation import TraceConfig, safe_json_dumps
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
from fi_instrumentation.fi_types import (
    EmbeddingAttributes,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
    ToolCallAttributes,
)
from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _ResponseAttributesExtractor:
    """Extract span attributes from Ollama response."""

    def __init__(self, config: Optional[TraceConfig] = None) -> None:
        self._config = config if config is not None else TraceConfig()

    def get_attributes(
        self,
        response: Optional[Dict[str, Any]],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from response."""
        if response is None:
            return

        # Token counts
        if "prompt_eval_count" in response:
            yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, response["prompt_eval_count"]
        if "eval_count" in response:
            yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, response["eval_count"]

        # Calculate total tokens
        prompt_tokens = response.get("prompt_eval_count") or 0
        completion_tokens = response.get("eval_count") or 0
        if prompt_tokens or completion_tokens:
            yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, prompt_tokens + completion_tokens

    def get_extra_attributes(
        self,
        response: Optional[Dict[str, Any]],
        request_parameters: Mapping[str, Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes including output messages."""
        if response is None:
            return

        # Chat response
        if "message" in response:
            message = response["message"]
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", message.get("role", "assistant")
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", message.get("content", "")
            yield from self._message_details(message)

        # Generate response
        elif "response" in response:
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "assistant"
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", response["response"]
            yield from self._message_details(response)

        # Embedding response
        elif "embedding" in response:
            embedding = response["embedding"]
            yield f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.0.{EmbeddingAttributes.EMBEDDING_VECTOR}", safe_json_dumps(embedding[:10])  # First 10 dims
            yield SpanAttributes.OUTPUT_VALUE, f"[{len(embedding)} dimensions]"

        # Multiple embeddings
        elif "embeddings" in response:
            embeddings = response["embeddings"]
            for i, emb in enumerate(embeddings[:5]):  # Limit to 5
                yield f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.{i}.{EmbeddingAttributes.EMBEDDING_VECTOR}", safe_json_dumps(emb[:10])
            yield SpanAttributes.OUTPUT_VALUE, f"[{len(embeddings)} embeddings]"

        # Core masks dedicated attributes but cannot inspect serialized JSON.
        # Apply directional privacy to a telemetry copy, not to SDK responses.
        output = dict(response)
        if self._config.hide_embedding_vectors:
            for key in ("embedding", "embeddings"):
                if key in output:
                    output[key] = REDACTED_VALUE
        if self._config.hide_output_messages:
            for key in ("message", "response", "thinking"):
                if key in output:
                    output[key] = REDACTED_VALUE
        elif self._config.hide_output_text:
            if isinstance(output.get("message"), dict):
                output["message"] = {
                    key: REDACTED_VALUE if key in ("content", "thinking") else value
                    for key, value in output["message"].items()
                }
            for key in ("response", "thinking"):
                if key in output:
                    output[key] = REDACTED_VALUE
        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(output)

        # Performance metrics (Ollama-specific)
        if "total_duration" in response:
            yield "ollama.total_duration_ns", response["total_duration"]
        if "load_duration" in response:
            yield "ollama.load_duration_ns", response["load_duration"]
        if "prompt_eval_duration" in response:
            yield "ollama.prompt_eval_duration_ns", response["prompt_eval_duration"]
        if "eval_duration" in response:
            yield "ollama.eval_duration_ns", response["eval_duration"]

        # Model info
        if "model" in response:
            yield SpanAttributes.GEN_AI_REQUEST_MODEL, response["model"]

        # Done reason
        if "done_reason" in response:
            yield "ollama.done_reason", response["done_reason"]

    def _message_details(self, message: Mapping[str, Any]) -> Iterator[Tuple[str, AttributeValue]]:
        prefix = f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0"
        if thinking := message.get("thinking"):
            content_prefix = f"{prefix}.{MessageAttributes.MESSAGE_CONTENTS}.0"
            yield f"{content_prefix}.{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "thinking"
            yield f"{content_prefix}.{MessageContentAttributes.MESSAGE_CONTENT_TEXT}", thinking
        for index, tool in enumerate(message.get("tool_calls") or ()):
            function = tool.get("function") or {}
            tool_prefix = f"{prefix}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}"
            if tool.get("id"):
                yield f"{tool_prefix}.{ToolCallAttributes.TOOL_CALL_ID}", tool["id"]
            if function.get("name"):
                yield f"{tool_prefix}.{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}", function["name"]
            if "arguments" in function:
                yield f"{tool_prefix}.{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}", safe_json_dumps(function["arguments"])
