# Changelog

## [0.1.0] - unreleased

- New package for AG2 1.x (PyPI `ag2`, import `ag2`). `setup()` attaches AG2's
  own `TelemetryMiddleware` with `capture_content=False` and registers the
  Future AGI exporter; `AG2SpanProcessor` sets `gen_ai.span.kind` from
  `gen_ai.operation.name` and aliases AG2's cache/thinking usage keys onto the
  GenAI semconv names (originals kept). Does not wrap `Agent` and never
  imports `autogen`. Refs: TH-8236.
