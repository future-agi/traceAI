import logging
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from fi_instrumentation import TraceConfig, safe_json_dumps
from fi_instrumentation.fi_types import (
    EmbeddingAttributes,
    SpanAttributes,
)
from opentelemetry.util.types import AttributeValue

from traceai_cohere._utils import _message_attributes

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def _without_citation_inputs(citations: Any) -> Any:
    """Remove source payloads, retaining generated citation text and references."""
    if not isinstance(citations, list):
        return citations
    result = []
    for citation in citations:
        if isinstance(citation, dict) and isinstance(citation.get("sources"), list):
            citation = dict(citation)
            citation["sources"] = [
                {
                    key: value
                    for key, value in source.items()
                    if key not in ("document", "tool_output")
                }
                if isinstance(source, dict)
                else source
                for source in citation["sources"]
            ]
        result.append(citation)
    return result


def _without_input_echoes(response: Dict[str, Any]) -> Dict[str, Any]:
    """Copy only telemetry paths containing known v1/v2 input echoes."""
    result = {
        key: value
        for key, value in response.items()
        if key not in ("chat_history", "documents")
    }
    if "citations" in result:
        result["citations"] = _without_citation_inputs(result["citations"])
    message = result.get("message")
    if isinstance(message, dict) and "citations" in message:
        result["message"] = {
            **message,
            "citations": _without_citation_inputs(message["citations"]),
        }
    return result


class _ChatResponseAttributesExtractor:
    """Extract span attributes from Cohere chat response."""

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

        # v1 reports meta; v2 reports usage. Prefer actual tokens over billed
        # units, and compute the total from the same source (including zeroes).
        usage = response.get("usage") or response.get("meta") or {}
        tokens = usage.get("tokens") or usage.get("billed_units") or {}
        for field, attribute in (
            ("input_tokens", SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS),
            ("output_tokens", SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS),
        ):
            if tokens.get(field) is not None:
                yield attribute, tokens[field]
        if any(
            tokens.get(field) is not None for field in ("input_tokens", "output_tokens")
        ):
            yield (
                SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS,
                (tokens.get("input_tokens") or 0) + (tokens.get("output_tokens") or 0),
            )

    def get_extra_attributes(
        self,
        response: Optional[Dict[str, Any]],
        request_parameters: Mapping[str, Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes including output."""
        if response is None:
            return

        message = response.get("message")
        if isinstance(message, dict):
            message = dict(message)
            content = message.get("content")
            if isinstance(content, list):
                message["content"] = "".join(
                    block.get("text") or ""
                    for block in content
                    if isinstance(block, dict)
                )
        else:
            message = {
                "role": "assistant",
                "content": response.get("text"),
                "tool_calls": response.get("tool_calls"),
            }
        yield from _message_attributes(
            message, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0"
        )
        if message.get("tool_calls"):
            yield "cohere.tool_calls_count", len(message["tool_calls"])
        citations = message.get("citations") or response.get("citations")
        if citations:
            yield "cohere.citations_count", len(citations)

        # Finish reason
        if "finish_reason" in response:
            yield "cohere.finish_reason", response.get("finish_reason", "")

        # Full responses duplicate message content (including history and tool
        # payloads). Do not export that copy when selective content is hidden.
        if self._config.hide_output_messages or self._config.hide_output_text:
            return
        # v1 echoes history/documents; v2 citation sources echo documents and
        # tool results. Input privacy applies to those structured copies, not
        # to the generated answer. Never mutate the caller's response.
        if (
            self._config.hide_inputs
            or self._config.hide_input_messages
            or self._config.hide_input_text
        ):
            response = _without_input_echoes(response)
        # Native message-end errors are data, not raised exceptions. Mask their
        # telemetry copy before JSON serialization, never the caller's event.
        if "error" in response:
            response = dict(response)
            error = self._config.mask("error.message", response.pop("error"))
            if error is not None:
                response["error"] = error
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
        meta = response.get("meta") or {}
        if "billed_units" in meta:
            billed = meta["billed_units"] or {}
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

        # Typed embeddings (v1 optional, v2 required) group vectors by encoding.
        embeddings = response.get("embeddings") or []
        if isinstance(embeddings, dict):
            embeddings = next(
                (vectors for vectors in embeddings.values() if vectors), []
            )
        count = len(embeddings)
        yield "cohere.embeddings_count", count
        if count and isinstance(embeddings[0], list):
            yield "cohere.embedding_dimensions", len(embeddings[0])
            yield (
                f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.0.{EmbeddingAttributes.EMBEDDING_VECTOR}",
                safe_json_dumps(embeddings[0][:10]),
            )
        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps({"embeddings_count": count})


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
        meta = response.get("meta") or {}
        if "billed_units" in meta:
            billed = meta["billed_units"] or {}
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

        yield (
            SpanAttributes.OUTPUT_VALUE,
            safe_json_dumps(
                [
                    {"index": r.get("index"), "score": r.get("relevance_score")}
                    for r in results[:5]
                ]
            ),
        )
        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response)
