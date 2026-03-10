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
        # For chat_stream and chat with message
        if "message" in request_parameters:
            message = request_parameters.get("message", "")
            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "user"
            yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", message
            yield SpanAttributes.INPUT_VALUE, message

        # Chat history
        if "chat_history" in request_parameters:
            chat_history = request_parameters.get("chat_history", [])
            for i, msg in enumerate(chat_history):
                role = msg.get("role", "")
                content = msg.get("message", "")
                yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_ROLE}", role
                yield f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_CONTENT}", content

        # Preamble (system prompt)
        if "preamble" in request_parameters:
            yield SpanAttributes.GEN_AI_PROVIDER_NAME_PROMPT, request_parameters.get("preamble", "")

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

    def get_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from request."""
        yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.RERANKER.value
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "cohere"
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "cohere"

        model = request_parameters.get("model", "rerank-english-v3.0")
        yield SpanAttributes.RERANKER_MODEL_NAME, model

        yield SpanAttributes.INPUT_MIME_TYPE, FiMimeTypeValues.JSON.value
        yield SpanAttributes.OUTPUT_MIME_TYPE, FiMimeTypeValues.JSON.value

        # Top N
        if "top_n" in request_parameters:
            yield SpanAttributes.RERANKER_TOP_K, request_parameters.get("top_n")

    def get_extra_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes."""
        query = request_parameters.get("query", "")
        yield SpanAttributes.RERANKER_QUERY, query
        yield SpanAttributes.INPUT_VALUE, query

        documents = request_parameters.get("documents", [])
        yield "cohere.documents_count", len(documents)
