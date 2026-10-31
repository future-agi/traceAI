"""Embedding support for the legacy SDK, which has no native OTel hooks."""

from fi_instrumentation.fi_types import FiSpanKindValues
from opentelemetry import trace

from ._chat_wrapper import _AsyncChatWrapper, _SyncChatWrapper


class _EmbeddingResponse:
    def _get_span_kind(self):
        return FiSpanKindValues.EMBEDDING.value

    def _finalize_response(self, response, with_span, request_parameters):
        # Never copy vectors into the generic output field: that would bypass
        # FI_HIDE_EMBEDDING_VECTORS and create very large attributes.
        attributes = dict(
            self._response_attributes_extractor.get_attributes_from_response(
                response, request_parameters
            )
        )
        with_span.finish_tracing(
            status=trace.Status(trace.StatusCode.OK), attributes=attributes
        )
        return response


class _EmbeddingWrapper(_EmbeddingResponse, _SyncChatWrapper):
    pass


class _AsyncEmbeddingWrapper(_EmbeddingResponse, _AsyncChatWrapper):
    pass
