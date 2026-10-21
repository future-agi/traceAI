## [1.1.1] - 2026-10-21
### Fixed
- `register()` no longer marks its own exporter as a default that the next
  `add_span_processor()` call replaces. Before, adding any span processor to the
  provider `register()` returns (a console exporter, a second backend, or a
  framework's own processor, such as LiveKit's privacy filter) shut the Future AGI
  exporter down and removed it, so traces silently stopped reaching Future AGI.
  Processors added later now run next to it, as in upstream OpenTelemetry.
  **Behaviour change:** code that relied on `add_span_processor()` to swap out
  `register()`'s exporter now exports to both; pass `register(span_exporter=...)`
  to export somewhere else instead.
- A `TracerProvider()` constructed directly is unchanged: its first
  `add_span_processor()` call still replaces the default `SimpleSpanProcessor`
  added on construction, and now logs that at INFO.

## [1.1.0] - 2026-08-11
### Feature
- `register()` now accepts span-processor / exporter / provider tuning:
  `max_queue_size`, `schedule_delay_millis`, `max_export_batch_size`,
  `export_timeout_millis`, `timeout`, `sampler`, `span_limits`, `span_exporter`.
  Unset values fall back to the OTEL env vars / upstream defaults (no behavior change).
- `BatchSpanProcessor` forwards its documented tuning params to the OpenTelemetry
  base class (previously silently dropped, causing unavoidable span drops under load).
- Re-exported `Sampler`, `ParentBased`, `TraceIdRatioBased`, `SpanLimits`.
### Fixed
- `register(batch=False)` warns when batch-only tuning params are passed.
- `register(span_exporter=...)` no longer crashes with non-OTLP exporters.
- Corrected docstrings that advertised an unsupported `endpoint` argument.

## [0.1.7] - 2025-06-10
### Feature
- Added support for ai-evaluation
- Added support for new evals in Prototype

## [0.1.6] - 2025-06-03
### Feature
- Added Support for Custom Evaluations in Prototype

## [0.1.5] - 2025-05-23
### Feature
- Added support for FutureAGI's protect
- Handling for incomplete spans during termination

## [0.1.4] - 2025-05-08
### Feature
- Bug fixes
- Support for new evals in Prototype

## [0.1.3] - 2025-04-14
### Feature
- Validations for Prototype eval mapping
- Added Support for Audio Evaluations