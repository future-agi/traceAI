## [0.1.0] - 2026-10-05
### Feature
- First release. OpenTelemetry instrumentation for the official Voyage AI
  Python client (`voyageai>=0.3.7,<1`, `fi-instrumentation-otel>=1.1.0`),
  tested on Python 3.10 to 3.13.
- `VoyageInstrumentor` wraps `Client` and `AsyncClient` `embed` (one
  `voyage.embed` EMBEDDING span per call) and `rerank` (one `voyage.rerank`
  RERANKER span per call). The client's own retries stay inside that span.
- Spans record the model, text and document counts, the requested and returned
  embedding dimensions, the host as `server.address`, and the client's
  `total_tokens` on `gen_ai.usage.input_tokens` and `gen_ai.usage.total_tokens`.
- The rerank span records the query and the relevance scores by default (PRD
  J2 / AC-03). Embed texts and rerank documents are recorded only with
  `instrument(capture_content=True)`. TraceConfig `hide_inputs` and
  `hide_outputs` drop them. Recorded content is capped at 64 items and 2 KB per
  string. Embedding vectors are never recorded.
- The Voyage API key is never exported. `pii_redaction` also covers the error
  status and the exception event.
- Errors set status ERROR and record one exception event. A cancelled async
  call or a Ctrl-C ends the span as `cancelled`.
- `multimodal_embed` and `contextualized_embed` are not traced in this release.
