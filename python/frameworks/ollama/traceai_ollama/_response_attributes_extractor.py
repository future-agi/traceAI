import logging
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    EmbeddingAttributes,
    MessageAttributes,
    SpanAttributes,
)
from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _ResponseAttributesExtractor:
    """Extract span attributes from Ollama response."""

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
        prompt_tokens = response.get("prompt_eval_count", 0)
        completion_tokens = response.get("eval_count", 0)
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
            yield SpanAttributes.OUTPUT_VALUE, message.get("content", "")

        # Generate response
        elif "response" in response:
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "assistant"
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", response["response"]
            yield SpanAttributes.OUTPUT_VALUE, response["response"]

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

        # Raw response for reference
        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response)

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
