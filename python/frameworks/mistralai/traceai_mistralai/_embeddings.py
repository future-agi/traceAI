"""Embedding support for the legacy SDK, which has no native OTel hooks."""

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    EmbeddingAttributes,
    FiSpanKindValues,
    SpanAttributes,
)
from opentelemetry import trace

from ._chat_wrapper import _AsyncChatWrapper, _SyncChatWrapper
from ._request_attributes_extractor import _get_model_attributes


class _EmbeddingResponse:
    def _get_span_kind(self):
        return FiSpanKindValues.EMBEDDING.value

    def _get_attributes_from_request(self, request_parameters):
        yield SpanAttributes.GEN_AI_SPAN_KIND, self._get_span_kind()
        inputs = request_parameters.get("inputs", [])
        if isinstance(inputs, str):
            inputs = [inputs]
        if isinstance(inputs, (list, tuple)):
            for index, text in enumerate(inputs):
                if isinstance(text, str):
                    yield (
                        f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.{index}."
                        f"{EmbeddingAttributes.EMBEDDING_TEXT}",
                        text,
                    )

    def _get_extra_attributes_from_request(self, request_parameters):
        yield from _get_model_attributes(request_parameters)
        # Text belongs only in the embedding attributes, where all three input
        # privacy flags apply. Do not duplicate it in input.value or parameters.
        yield (
            SpanAttributes.GEN_AI_REQUEST_PARAMETERS,
            safe_json_dumps(
                {
                    key: value
                    for key, value in request_parameters.items()
                    if key != "inputs"
                }
            ),
        )

    def _finalize_response(self, response, with_span, request_parameters):
        # Never copy vectors into the generic output field: that would bypass
        # FI_HIDE_EMBEDDING_VECTORS and create very large attributes.
        attributes = dict(
            self._response_attributes_extractor.get_attributes_from_response(
                response, request_parameters
            )
        )
        with_span.finish_tracing(
            status=trace.Status(trace.StatusCode.OK), extra_attributes=attributes
        )
        return response


class _EmbeddingWrapper(_EmbeddingResponse, _SyncChatWrapper):
    pass


class _AsyncEmbeddingWrapper(_EmbeddingResponse, _AsyncChatWrapper):
    pass
