# Changelog

## [0.1.0] - unreleased

- New package for AG2 1.x (PyPI `ag2`, import `ag2`). `setup()` attaches AG2's
  own `TelemetryMiddleware` with `capture_content=False` and registers the
  Future AGI exporter; `AG2SpanProcessor` sets `gen_ai.span.kind` from
  `gen_ai.operation.name` and aliases AG2's cache/thinking usage keys onto the
  GenAI semconv names (originals kept). Does not wrap `Agent` and never
  imports `autogen`. Refs: TH-8236.
- Token totals: Future AGI sums the promoted `gen_ai.usage.input_tokens` /
  `output_tokens` / `total_tokens` over every span of a trace. The processor
  moves those keys to `ag2.usage.*` (values kept) on `record_usage model_call`
  spans, which repeat the chat span's tokens, and on `record_usage subtask`
  rollups whose sub-agent (`ag2.usage.label`) is itself instrumented in the
  same trace. `aggregation` and `compaction` usage keeps its promoted tokens:
  AG2 calls the model outside the middleware for those, so the usage span is
  the only copy. The trace total now equals AG2's `UsageReport` total.
- Requires `fi-instrumentation-otel>=1.1.0` (was `>=1.0.0`): published 1.0.0
  crashes in `register()` with `opentelemetry-exporter-otlp-proto-http` 1.45.0
  (`AttributeError: 'HTTPSpanExporter' object has no attribute '_headers'`).
