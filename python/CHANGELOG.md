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