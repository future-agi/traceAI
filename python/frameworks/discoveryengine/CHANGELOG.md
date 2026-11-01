# Changelog

## 0.1.0

First release.

- Trace `google-cloud-discoveryengine` v1 `SearchServiceClient.search`,
  `SearchServiceClient.search_lite` and
  `ConversationalSearchServiceClient.answer_query`, and their async twins:
  one span per call with `gen_ai.span.kind` = `RETRIEVER`, `discoveryengine.serving_config` and
  `discoveryengine.result_count`; for answers also
  `discoveryengine.answer.length`, `discoveryengine.answer.state` and
  `discoveryengine.session`. No model name, tokens or cost.
- The query follows `TraceConfig`: recorded by default
  (`gen_ai.retrieval.query`, `input.value`), hidden with `FI_HIDE_INPUTS` /
  `hide_inputs`, and then also removed from error text, verbatim and in the
  escaped copies the client libraries write. No package-specific switch (a
  pre-release `capture_query` option is ignored with a warning). Results and
  answer text are never recorded.
- The `exception` event carries the type and the cleaned message only; no
  `exception.stacktrace` (traceback frames hold local file paths).
- REST transport: `discoveryengine.error.status` comes from the canonical
  `status` of the JSON error body (`google-api-core` raises REST errors by
  HTTP code, for example `Forbidden`, which has no gRPC status).
- Google credentials (transport credentials, read again when an error is
  recorded; `client_options.api_key`; per-call auth metadata; and Google
  token shapes, including JWTs) are removed from every recorded value.
  Errors record the gRPC status and code, then re-raise.
- `TraceConfig` `hide_inputs` and `pii_redaction` apply, including to error
  text. Spans come from `FITracer`, so `using_*` context attributes apply.
- Accepts `google-cloud-discoveryengine>=0.20.5,<1`; tested with 0.20.5 on
  Python 3.10 to 3.13.
- Checked against the live v1 API (2026-10-18): `search` on the gRPC, REST
  and gRPC asyncio transports, and real `NOT_FOUND` and
  `FAILED_PRECONDITION` errors from `search`, `search_lite` and
  `answer_query`. The REST tests replay those error bodies.
