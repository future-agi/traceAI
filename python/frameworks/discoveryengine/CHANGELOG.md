# Changelog

## 0.1.0 (unreleased)

First release.

- Trace `google-cloud-discoveryengine` v1 `SearchServiceClient.search`,
  `SearchServiceClient.search_lite` and
  `ConversationalSearchServiceClient.answer_query`, and their async twins:
  one `RETRIEVER` span per call with `discoveryengine.serving_config` and
  `discoveryengine.result_count`; for answers also
  `discoveryengine.answer.length`, `discoveryengine.answer.state` and
  `discoveryengine.session`. No model name, tokens or cost.
- Query text only with `capture_query=True`; off by default, and removed
  from error text whenever it is not recorded. Results and answer text are
  never recorded.
- Google credentials (transport credentials, `client_options.api_key`,
  per-call auth metadata, and Google token shapes) are removed from every
  recorded value. Errors record the gRPC status and code, then re-raise.
- `TraceConfig` `hide_inputs` and `pii_redaction` apply, including to error
  text. Spans come from `FITracer`, so `using_*` context attributes apply.
- Accepts `google-cloud-discoveryengine>=0.20.5,<1`; tested with 0.20.5 on
  Python 3.10 to 3.13.
