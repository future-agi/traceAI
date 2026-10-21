## [0.1.1] - 2026-10-21
### Fixed
- `configure_strands_tracing()` no longer adds Strands' OTLP span exporter when it would only send copies of what the
  tracer provider already sends. With the provider `fi_instrumentation.register()` returns and `OTEL_EXPORTER_OTLP_*`
  pointing at Future AGI, every span reached Future AGI twice once fi-instrumentation-otel stopped dropping
  `register()`'s exporter when another span processor is added (TH-8727). The exporter is skipped, with an INFO log line,
  only when all of these hold:
  - a span processor on the provider already exports to the same traces URL (scheme, host, port, path and query,
    normalised; credentials in the URL must match too);
  - that exporter already sends every header Strands' exporter would send, with the same value, and no client
    certificate or credential provider is set for Strands' exporter;
  - the provider keeps that exporter when another span processor is added. On fi-instrumentation-otel 1.1.0 and
    earlier it does not, so there Strands' exporter is added exactly as in 0.1.0.

  Strands' OTLP exporter is still added in every other case, including the default `localhost:4318`, and the console
  exporter and meter are set up as before.

## [0.1.0]
### Feature
- Added support for AWS Strands Agents instrumentation.
