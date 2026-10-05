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
- A later `setup(..., config=...)` on a provider that already has an
  `AG2SpanProcessor` now replaces that processor's `TraceConfig` (most recent
  call wins, warning logged when it changes) instead of being ignored;
  `config=None` keeps the installed config.
- traceAI context helpers (`using_session`, `using_user`, `using_metadata`,
  `using_attributes`, ...) now reach AG2 spans: `AG2SpanProcessor.on_start`
  copies `get_attributes_from_context()` without overriding keys AG2 sets,
  except `session.id`, where the context wins over `span_attributes`.
- `TraceConfig` text flags now act on AG2 content. AG2 records messages as one
  JSON string per span, which `TraceConfig.mask`'s `hide_input_text` /
  `hide_output_text` rules never matched, so those flags used to leave content
  in place. They now drop the whole input-side / output-side content
  attributes (messages, system instructions, tool arguments/results,
  human-input text), as `hide_inputs` / `hide_outputs` do;
  `hide_input_messages` also drops `gen_ai.system_instructions`. The README
  lists which flags apply; `hide_input_images` and `hide_embedding_vectors`
  have nothing to act on.
