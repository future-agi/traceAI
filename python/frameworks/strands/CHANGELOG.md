## [0.1.1] - 2026-10-21
### Fixed
- `configure_strands_tracing()` no longer adds Strands' OTLP span exporter when it would only send copies of what the
  tracer provider already sends. With the provider `fi_instrumentation.register()` returns and `OTEL_EXPORTER_OTLP_*`
  pointing at Future AGI, every span reached Future AGI twice once fi-instrumentation-otel stopped dropping
  `register()`'s exporter when another span processor is added (TH-8727). The exporter is skipped, with an INFO log line,
  only when all of these hold:
  - a span processor on the provider already exports to the same traces URL (scheme, host, port, path and query,
    normalised; credentials in the URL must match too);
  - that exporter sends exactly the same header set as Strands' exporter: the same names (case-insensitively) and
    values, with no header missing on either side. An extra routing header on either exporter keeps Strands' exporter;
    a client certificate or credential provider set for it does too;
  - the provider keeps that exporter when another span processor is added. On fi-instrumentation-otel 1.1.0 and
    earlier it does not, so there Strands' exporter is added exactly as in 0.1.0.

  Strands' OTLP exporter is still added in every other case, including the default `localhost:4318`, and the console
  exporter and meter are set up as before.

- README Option 1 and the examples now pass `set_global_tracer_provider=True` to `register()`. Strands creates its
  spans from OpenTelemetry's global tracer provider and `configure_strands_tracing(tracer_provider=...)` does not
  make the provider global, so with `register()`'s default (`False`) a Strands agent sent no spans to Future AGI.

## [0.1.0]
### Feature
- Added support for AWS Strands Agents instrumentation.
