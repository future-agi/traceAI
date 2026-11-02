import logging
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from fi_instrumentation import TraceConfig, safe_json_dumps
from fi_instrumentation.fi_types import (
    FiMimeTypeValues,
    FiSpanKindValues,
    ImageAttributes,
    MessageAttributes,
    MessageContentAttributes,
    RerankerAttributes,
    SpanAttributes,
)
from opentelemetry.util.types import AttributeValue

from traceai_cohere._utils import _message_attributes, _to_dict

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _ChatRequestAttributesExtractor:
    """Extract span attributes from Cohere chat request."""

    def __init__(self, config: Optional[TraceConfig] = None) -> None:
        self._config = config if config is not None else TraceConfig()

    def _mask_message_images(self, message: Any, index: int) -> Dict[str, Any]:
        # Both input.value and message.content serialize entire content blocks.
        # Apply the core's image mask before either copy loses its semantic keys.
        # Copy only telemetry data: never redact the request sent to Cohere.
        message = dict(_to_dict(message))
        content = message.get("content")
        if not isinstance(content, (list, tuple)):
            return message
        masked_content = []
        for block_index, item in enumerate(content):
            block = _to_dict(item)
            if block.get("type") == "image_url":
                image = _to_dict(block.get("image_url"))
                key = (
                    f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{index}."
                    f"{MessageAttributes.MESSAGE_CONTENTS}.{block_index}."
                    f"{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}."
                    f"{ImageAttributes.IMAGE_URL}"
                )
                url = self._config.mask(key, image.get("url"))
                if url is None:
                    continue
                block = {**block, "image_url": {**image, "url": url}}
            masked_content.append(block)
        message["content"] = masked_content
        return message

    def get_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from request."""
        yield SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value
        yield SpanAttributes.GEN_AI_PROVIDER_NAME, "cohere"

        model = request_parameters.get("model", "command-r-plus")
        yield SpanAttributes.GEN_AI_REQUEST_MODEL, model

        yield SpanAttributes.INPUT_MIME_TYPE, FiMimeTypeValues.JSON.value
        yield SpanAttributes.OUTPUT_MIME_TYPE, FiMimeTypeValues.JSON.value

        # Invocation parameters
        invocation_params = self._extract_invocation_parameters(request_parameters)
        if invocation_params:
            yield (
                SpanAttributes.GEN_AI_REQUEST_PARAMETERS,
                safe_json_dumps(invocation_params),
            )

    def get_extra_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes including messages."""
        # Keep system/history/current messages in order without overwriting
        # indices. Canonical input-message keys also honor FI_HIDE_INPUTS.
        messages = []
        if "preamble" in request_parameters:
            messages.append(
                {"role": "system", "message": request_parameters["preamble"]}
            )
        messages.extend((request_parameters.get("chat_history") or []))
        if "message" in request_parameters:
            message = request_parameters.get("message", "")
            messages.append({"role": "user", "message": message})
            if not (self._config.hide_input_messages or self._config.hide_input_text):
                yield SpanAttributes.INPUT_VALUE, message

        if "messages" in request_parameters:
            messages = [
                self._mask_message_images(message, i)
                for i, message in enumerate(request_parameters.get("messages") or [])
            ]
            # Raw JSON is a second copy of message content. Omit it under
            # selective content privacy; canonical fields retain safe metadata.
            if not (self._config.hide_input_messages or self._config.hide_input_text):
                yield SpanAttributes.INPUT_VALUE, safe_json_dumps(messages)
        for i, msg in enumerate(messages):
            yield from _message_attributes(
                msg, f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{i}"
            )

        # Documents for RAG
        if "documents" in request_parameters:
            docs = request_parameters.get("documents") or []
            yield "cohere.documents_count", len(docs)

        # Tools
        if "tools" in request_parameters:
            tools = request_parameters.get("tools") or []
            yield "cohere.tools_count", len(tools)

    def _extract_invocation_parameters(
        self, request_parameters: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Extract model invocation parameters."""
        params = {}
        param_keys = [
            "temperature",
            "max_tokens",
            "k",
            "p",
            "stop_sequences",
            "frequency_penalty",
            "presence_penalty",
            "seed",
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

        model = request_parameters.get("model", "embed-english-v3.0")
        yield SpanAttributes.EMBEDDING_MODEL_NAME, model

        yield SpanAttributes.INPUT_MIME_TYPE, FiMimeTypeValues.JSON.value
        yield SpanAttributes.OUTPUT_MIME_TYPE, FiMimeTypeValues.JSON.value

        # Input type
        if "input_type" in request_parameters:
            yield "cohere.input_type", request_parameters.get("input_type")

        # Embedding types
        if "embedding_types" in request_parameters:
            yield (
                "cohere.embedding_types",
                safe_json_dumps(request_parameters.get("embedding_types")),
            )

    def get_extra_attributes_from_request(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes."""
        texts = request_parameters.get("texts")
        images = request_parameters.get("images")
        inputs = request_parameters.get("inputs")

        # Cohere v2 accepts image-only requests and, in newer SDKs, inputs
        # containing both text and image components. Never serialize input
        # objects here: they may contain private image data URIs.
        plain_text_count = len(texts) if isinstance(texts, (list, tuple)) else 0
        plain_image_count = len(images) if isinstance(images, (list, tuple)) else 0
        mixed_count = len(inputs) if isinstance(inputs, (list, tuple)) else 0
        text_count = plain_text_count
        image_count = plain_image_count
        for item in inputs if isinstance(inputs, (list, tuple)) else ():
            content = _to_dict(item).get("content")
            for block in content if isinstance(content, (list, tuple)) else ():
                modality = _to_dict(block).get("type")
                if modality == "text":
                    text_count += 1
                elif modality == "image_url":
                    image_count += 1

        if texts is not None or text_count:
            yield "cohere.texts_count", text_count
        if images is not None or image_count:
            yield "cohere.images_count", image_count
        if images is not None or inputs is not None:
            # Mixed items each produce one embedding even with two components.
            input_count = plain_text_count + plain_image_count + mixed_count
            yield "cohere.inputs_count", input_count
            yield SpanAttributes.INPUT_VALUE, safe_json_dumps(
                {"texts_count": text_count, "images_count": image_count,
                 "inputs_count": input_count}
            )
        elif isinstance(texts, (list, tuple)):
            yield SpanAttributes.INPUT_VALUE, safe_json_dumps(texts[:5])  # Limit
        elif texts is not None:
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

        documents = request_parameters.get("documents") or []
        yield "cohere.documents_count", len(documents)
