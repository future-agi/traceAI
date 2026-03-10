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


class _ChatResponseAttributesExtractor:
    """Extract span attributes from Cohere chat response."""

    def get_attributes(
        self,
        response: Optional[Dict[str, Any]],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from response."""
        if response is None:
            return

        # Token counts from meta
        meta = response.get("meta", {})
        if "billed_units" in meta:
            billed = meta["billed_units"]
            if "input_tokens" in billed:
                yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, billed["input_tokens"]
            if "output_tokens" in billed:
                yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, billed["output_tokens"]

            total = billed.get("input_tokens", 0) + billed.get("output_tokens", 0)
            if total:
                yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, total

        if "tokens" in meta:
            tokens = meta["tokens"]
            if "input_tokens" in tokens:
                yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, tokens["input_tokens"]
            if "output_tokens" in tokens:
                yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, tokens["output_tokens"]

    def get_extra_attributes(
        self,
        response: Optional[Dict[str, Any]],
        request_parameters: Mapping[str, Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes including output."""
        if response is None:
            return

        # Text response
        if "text" in response:
            text = response.get("text", "")
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "assistant"
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", text
            yield SpanAttributes.OUTPUT_VALUE, text

        # Tool calls
        if "tool_calls" in response and response["tool_calls"]:
            tool_calls = response["tool_calls"]
            yield "cohere.tool_calls_count", len(tool_calls)
            for i, tc in enumerate(tool_calls[:5]):  # Limit
                yield f"cohere.tool_calls.{i}.name", tc.get("name", "")
                yield f"cohere.tool_calls.{i}.parameters", safe_json_dumps(tc.get("parameters", {}))

        # Citations
        if "citations" in response and response["citations"]:
            yield "cohere.citations_count", len(response["citations"])

        # Finish reason
        if "finish_reason" in response:
            yield "cohere.finish_reason", response.get("finish_reason", "")

        # Raw response
        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response)


class _EmbedResponseAttributesExtractor:
    """Extract span attributes from Cohere embed response."""

    def get_attributes(
        self,
        response: Optional[Dict[str, Any]],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from response."""
        if response is None:
            return

        # Token count from meta
        meta = response.get("meta", {})
        if "billed_units" in meta:
            billed = meta["billed_units"]
            if "input_tokens" in billed:
                yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, billed["input_tokens"]

    def get_extra_attributes(
        self,
        response: Optional[Dict[str, Any]],
        request_parameters: Mapping[str, Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes."""
        if response is None:
            return

        # Embeddings
        embeddings = response.get("embeddings", [])
        if embeddings:
            yield "cohere.embeddings_count", len(embeddings)
            # First embedding info
            if len(embeddings) > 0 and isinstance(embeddings[0], list):
                yield "cohere.embedding_dimensions", len(embeddings[0])
                yield f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.0.{EmbeddingAttributes.EMBEDDING_VECTOR}", safe_json_dumps(embeddings[0][:10])

        yield SpanAttributes.OUTPUT_VALUE, f"[{len(embeddings)} embeddings]"
        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps({"embeddings_count": len(embeddings)})


class _RerankResponseAttributesExtractor:
    """Extract span attributes from Cohere rerank response."""

    def get_attributes(
        self,
        response: Optional[Dict[str, Any]],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from response."""
        if response is None:
            return

        # Token count from meta
        meta = response.get("meta", {})
        if "billed_units" in meta:
            billed = meta["billed_units"]
            if "search_units" in billed:
                yield "cohere.search_units", billed["search_units"]

    def get_extra_attributes(
        self,
        response: Optional[Dict[str, Any]],
        request_parameters: Mapping[str, Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes."""
        if response is None:
            return

        # Results
        results = response.get("results", [])
        yield "cohere.results_count", len(results)

        # Extract relevance scores
        scores = []
        for i, result in enumerate(results[:10]):  # Limit to 10
            index = result.get("index", i)
            score = result.get("relevance_score", 0.0)
            scores.append(score)
            yield f"cohere.rerank.{i}.index", index
            yield f"cohere.rerank.{i}.relevance_score", score

        if scores:
            yield "cohere.rerank.scores", safe_json_dumps(scores)

        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps([{"index": r.get("index"), "score": r.get("relevance_score")} for r in results[:5]])
        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response)
