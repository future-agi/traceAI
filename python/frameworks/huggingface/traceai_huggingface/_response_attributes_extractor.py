import logging
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    EmbeddingAttributes,
    MessageAttributes,
    SpanAttributes,
)
from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _TextGenerationResponseAttributesExtractor:
    """Extract span attributes from HuggingFace text_generation response."""

    def get_attributes(
        self,
        response: Optional[Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from response."""
        if response is None:
            return

        # For TextGenerationOutput or TextGenerationStreamOutput
        if hasattr(response, "details") and response.details:
            details = response.details
            # Token counts
            if hasattr(details, "generated_tokens"):
                yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, details.generated_tokens
            if hasattr(details, "prefill") and details.prefill:
                yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, len(details.prefill)

        # For dict response
        if isinstance(response, dict):
            details = response.get("details", {})
            if details:
                if "generated_tokens" in details:
                    yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, details["generated_tokens"]

    def get_extra_attributes(
        self,
        response: Optional[Any],
        request_parameters: Mapping[str, Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes including output."""
        if response is None:
            return

        # Get generated text
        generated_text = ""
        if hasattr(response, "generated_text"):
            generated_text = response.generated_text or ""
        elif isinstance(response, dict):
            generated_text = response.get("generated_text", "")
        elif isinstance(response, str):
            generated_text = response

        if generated_text:
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "assistant"
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", generated_text
            yield SpanAttributes.OUTPUT_VALUE, generated_text

        # Finish reason
        finish_reason = None
        if hasattr(response, "details") and response.details:
            if hasattr(response.details, "finish_reason"):
                finish_reason = response.details.finish_reason
        elif isinstance(response, dict):
            details = response.get("details", {})
            if details:
                finish_reason = details.get("finish_reason")

        if finish_reason:
            yield "huggingface.finish_reason", str(finish_reason)

        # Raw response
        if isinstance(response, dict):
            yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response)
        elif hasattr(response, "model_dump"):
            yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response.model_dump())
        elif hasattr(response, "__dict__"):
            yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response.__dict__)


class _ChatCompletionResponseAttributesExtractor:
    """Extract span attributes from HuggingFace chat_completion response."""

    def get_attributes(
        self,
        response: Optional[Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from response."""
        if response is None:
            return

        # Token counts from usage
        usage = None
        if hasattr(response, "usage"):
            usage = response.usage
        elif isinstance(response, dict):
            usage = response.get("usage", {})

        if usage:
            if hasattr(usage, "prompt_tokens"):
                yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, usage.prompt_tokens
            elif isinstance(usage, dict) and "prompt_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, usage["prompt_tokens"]

            if hasattr(usage, "completion_tokens"):
                yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, usage.completion_tokens
            elif isinstance(usage, dict) and "completion_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, usage["completion_tokens"]

            if hasattr(usage, "total_tokens"):
                yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, usage.total_tokens
            elif isinstance(usage, dict) and "total_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, usage["total_tokens"]

    def get_extra_attributes(
        self,
        response: Optional[Any],
        request_parameters: Mapping[str, Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes including output."""
        if response is None:
            return

        # Get choices
        choices = []
        if hasattr(response, "choices"):
            choices = response.choices or []
        elif isinstance(response, dict):
            choices = response.get("choices", [])

        for i, choice in enumerate(choices):
            message = None
            if hasattr(choice, "message"):
                message = choice.message
            elif isinstance(choice, dict):
                message = choice.get("message", {})

            if message:
                role = ""
                content = ""
                if hasattr(message, "role"):
                    role = message.role
                elif isinstance(message, dict):
                    role = message.get("role", "")

                if hasattr(message, "content"):
                    content = message.content or ""
                elif isinstance(message, dict):
                    content = message.get("content", "")

                yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_ROLE}", role
                yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_CONTENT}", content

                if i == 0:
                    yield SpanAttributes.OUTPUT_VALUE, content

                # Tool calls
                tool_calls = None
                if hasattr(message, "tool_calls"):
                    tool_calls = message.tool_calls
                elif isinstance(message, dict):
                    tool_calls = message.get("tool_calls")

                if tool_calls:
                    yield f"huggingface.choices.{i}.tool_calls_count", len(tool_calls)
                    for j, tc in enumerate(tool_calls[:5]):  # Limit to 5
                        if hasattr(tc, "function"):
                            func = tc.function
                            if hasattr(func, "name"):
                                yield f"huggingface.choices.{i}.tool_calls.{j}.name", func.name
                            if hasattr(func, "arguments"):
                                yield f"huggingface.choices.{i}.tool_calls.{j}.arguments", func.arguments
                        elif isinstance(tc, dict):
                            func = tc.get("function", {})
                            yield f"huggingface.choices.{i}.tool_calls.{j}.name", func.get("name", "")
                            yield f"huggingface.choices.{i}.tool_calls.{j}.arguments", func.get("arguments", "")

            # Finish reason
            finish_reason = None
            if hasattr(choice, "finish_reason"):
                finish_reason = choice.finish_reason
            elif isinstance(choice, dict):
                finish_reason = choice.get("finish_reason")

            if finish_reason:
                yield f"huggingface.choices.{i}.finish_reason", str(finish_reason)

        # Raw response
        if isinstance(response, dict):
            yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response)
        elif hasattr(response, "model_dump"):
            yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response.model_dump())
        elif hasattr(response, "__dict__"):
            yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response.__dict__)


class _FeatureExtractionResponseAttributesExtractor:
    """Extract span attributes from HuggingFace feature_extraction response."""

    def get_attributes(
        self,
        response: Optional[Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from response."""
        if response is None:
            return
        # Feature extraction doesn't typically return token counts

    def get_extra_attributes(
        self,
        response: Optional[Any],
        request_parameters: Mapping[str, Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes."""
        if response is None:
            return

        # Response is typically a list of embeddings (nested lists)
        embeddings = response
        if isinstance(embeddings, list):
            # Check if it's a single embedding or multiple
            if embeddings and isinstance(embeddings[0], (list, tuple)):
                # Multiple embeddings or single embedding with multiple dimensions
                if embeddings[0] and isinstance(embeddings[0][0], (int, float)):
                    # Single text input: [[dim1, dim2, ...]]
                    yield "huggingface.embeddings_count", 1
                    yield "huggingface.embedding_dimensions", len(embeddings[0])
                    yield f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.0.{EmbeddingAttributes.EMBEDDING_VECTOR}", safe_json_dumps(embeddings[0][:10])
                elif embeddings[0] and isinstance(embeddings[0][0], list):
                    # Multiple text inputs: [[[dim1, dim2, ...]], [[dim1, dim2, ...]]]
                    yield "huggingface.embeddings_count", len(embeddings)
                    if embeddings[0][0]:
                        yield "huggingface.embedding_dimensions", len(embeddings[0][0])
                        yield f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.0.{EmbeddingAttributes.EMBEDDING_VECTOR}", safe_json_dumps(embeddings[0][0][:10])
            elif embeddings and isinstance(embeddings[0], (int, float)):
                # Single dimension list
                yield "huggingface.embeddings_count", 1
                yield "huggingface.embedding_dimensions", len(embeddings)
                yield f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.0.{EmbeddingAttributes.EMBEDDING_VECTOR}", safe_json_dumps(embeddings[:10])

            yield SpanAttributes.OUTPUT_VALUE, f"[embeddings generated]"
        else:
            yield SpanAttributes.OUTPUT_VALUE, str(response)[:500]

        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps({"type": "embedding", "shape": str(type(response))})
