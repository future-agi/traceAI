## [0.1.0] - 2026-10-04
### Features
- New package for AG2 Classic (`autogen` 0.14.x). `setup()` enables upstream
  `autogen.opentelemetry` (`instrument_llm_wrapper`, `instrument_agent`,
  `instrument_pattern`) on the Future AGI tracer provider, without creating a
  second provider or exporter.
- `AG2ClassicSpanProcessor` maps `ag2.span.type` to `gen_ai.span.kind`, copies
  the outermost `gen_ai.conversation.id` to `session.id`, adds
  `gen_ai.cost.total` (only when upstream's cost is above 0; AG2 reports 0
  for models it cannot price) and `gen_ai.usage.total_tokens` on LLM spans,
  and sets ERROR status from upstream `error.type`.
- Content follows the traceAI `TraceConfig` convention: captured by default,
  hidden with `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` (and the message/text
  variants) or `setup(config=TraceConfig(...))`. Upstream message, tool,
  human-input and code-output keys of a hidden side are removed before export.
  `capture_content=True/False` is an optional override.
- Version guard: rejects non-0.14 `autogen`, Microsoft AutoGen, `ag2` 1.x and
  `pyautogen` layouts with an error that names the matching package.
