## [0.1.1] - 2026-10-21
### Fixed
- `configure_strands_tracing()` no longer adds Strands' OTLP span exporter for a collector the tracer provider already
  exports to. With the provider `fi_instrumentation.register()` returns and `OTEL_EXPORTER_OTLP_*` pointing at Future AGI,
  every span reached Future AGI twice once fi-instrumentation-otel stopped dropping `register()`'s exporter when another
  span processor is added (TH-8727). The skip is logged at INFO. Strands' OTLP exporter is still added for any other
  collector, including the default `localhost:4318`, and the console exporter and meter are set up as before.

## [0.1.0]
### Feature
- Added support for AWS Strands Agents instrumentation.
