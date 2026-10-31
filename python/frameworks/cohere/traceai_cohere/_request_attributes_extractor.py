import logging
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from fi_instrumentation import TraceConfig, safe_json_dumps
from fi_instrumentation.fi_types import (
    FiMimeTypeValues,
    FiSpanKindValues,
    MessageAttributes,
    RerankerAttributes,
    SpanAttributes,
)
from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _ChatRequestAttributesExtractor:
    """Extract span attributes from Cohere chat request."""

    def get_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from request."""
        yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "cohere"
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "cohere"

        model = request_parameters.get("model", "command-r-plus")
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
        # Keep system/history/current messages in order without overwriting
        # indices. Canonical input-message keys also honor FI_HIDE_INPUTS.
        messages = []
        if "preamble" in request_parameters:
            messages.append({"role": "system", "message": request_parameters["preamble"]})
        messages.extend(request_parameters.get("chat_history", []))
        if "message" in request_parameters:
            message = request_parameters.get("message", "")
            messages.append({"role": "user", "message": message})
            yield SpanAttributes.INPUT_VALUE, message

        for i, msg in enumerate(messages):
            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_ROLE}", msg.get("role", "")
            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_CONTENT}", msg.get("message", "")

        # Documents for RAG
        if "documents" in request_parameters:
            docs = request_parameters.get("documents", [])
            yield "cohere.documents_count", len(docs)

        # Tools
        if "tools" in request_parameters:
            tools = request_parameters.get("tools", [])
            yield "cohere.tools_count", len(tools)

    def _extract_invocation_parameters(
        self, request_parameters: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Extract model invocation parameters."""
        params = {}
        param_keys = [
            "temperature", "max_tokens", "k", "p", "stop_sequences",
            "frequency_penalty", "presence_penalty", "seed"
        ]
        for key in param_keys:
            if key in request_parameters:
                params[key] = request_parameters[key]
        return params


class _EmbedRequestAttributesExtractor:
    """Extract span attributes from Cohere embed request."""

    def get_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from request."""
        yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.EMBEDDING.value
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "cohere"
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "cohere"

        model = request_parameters.get("model", "embed-english-v3.0")
        yield SpanAttributes.EMBEDDING_MODEL_NAME, model

        yield SpanAttributes.INPUT_MIME_TYPE, FiMimeTypeValues.JSON.value
        yield SpanAttributes.OUTPUT_MIME_TYPE, FiMimeTypeValues.JSON.value

        # Input type
        if "input_type" in request_parameters:
            yield "cohere.input_type", request_parameters.get("input_type")

        # Embedding types
        if "embedding_types" in request_parameters:
            yield "cohere.embedding_types", safe_json_dumps(request_parameters.get("embedding_types"))

    def get_extra_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes."""
        texts = request_parameters.get("texts", [])
        if isinstance(texts, list):
            yield SpanAttributes.INPUT_VALUE, safe_json_dumps(texts[:5])  # Limit
            yield "cohere.texts_count", len(texts)
        else:
            yield SpanAttributes.INPUT_VALUE, str(texts)


class _RerankRequestAttributesExtractor:
    """Extract span attributes from Cohere rerank request."""

    def __init__(self, config: Optional[TraceConfig] = None) -> None:
        self._config = config if config is not None else TraceConfig()

    def get_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from request."""
        yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.RERANKER.value
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "cohere"
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "cohere"

        model = request_parameters.get("model", "rerank-english-v3.0")
        yield RerankerAttributes.RERANKER_MODEL_NAME, model

        yield SpanAttributes.INPUT_MIME_TYPE, FiMimeTypeValues.JSON.value
        yield SpanAttributes.OUTPUT_MIME_TYPE, FiMimeTypeValues.JSON.value

        # Top N
        if "top_n" in request_parameters:
            yield RerankerAttributes.RERANKER_TOP_K, request_parameters.get("top_n")

    def get_extra_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes."""
        query = request_parameters.get("query", "")
        # This semantic alias is not covered by older core mask tables.
        masked_query = self._config.mask(SpanAttributes.INPUT_VALUE, query)
        if masked_query is not None:
            yield RerankerAttributes.RERANKER_QUERY, masked_query
        yield SpanAttributes.INPUT_VALUE, query

        documents = request_parameters.get("documents", [])
        yield "cohere.documents_count", len(documents)
