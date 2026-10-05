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
- A later `trace_provider.add_span_processor(...)` no longer removes
  `AG2SpanProcessor`. fi's provider shuts down and clears every processor on
  the first such call after `register()`; `install_span_processor` now wraps
  the provider instance's `add_span_processor` so the AG2 processor sits out
  that reset and is put back first. Installing on a provider that uses
  `ConcurrentMultiSpanProcessor` logs a warning (normalization before export
  is not guaranteed there).
- Normalized spans keep the SDK's `BoundedAttributes` instead of a plain
  dict, so `dropped_attributes_count` is no longer reset to 0 and the span's
  attribute limits still apply (a key the processor adds to a full span
  evicts the oldest one and is counted).
- `setup()` without `tracer_provider` now registers a provider once and
  reuses it on later calls without one (until it is shut down) instead of
  calling `register()` again. Before, `setup(planner)` then `setup(worker)`
  gave each agent its own provider and `AG2SpanProcessor`, so the planner's
  processor never saw the worker's `invoke_agent` span and the worker's
  `record_usage subtask` rollup was counted on top of the worker's chat spans.
  A different `project_name` on a later call is ignored with a warning.
