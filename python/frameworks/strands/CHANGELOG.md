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

## [0.1.2](https://github.com/future-agi/traceAI/compare/traceai-strands-v0.1.1...traceai-strands-v0.1.2) (2026-11-05)


### Bug Fixes

* **strands:** adapt native spans without duplicate usage ([#470](https://github.com/future-agi/traceAI/issues/470)) ([8d761fe](https://github.com/future-agi/traceAI/commit/8d761fe8090097815d3746c0918431893af160c9))
* **strands:** preserve native completion and request metadata ([#522](https://github.com/future-agi/traceAI/issues/522)) ([6d7fb5d](https://github.com/future-agi/traceAI/commit/6d7fb5d5399d962742bc8d64434f00fdc2f41660))
* **strands:** require strands-agents 1.24 and document early stream-close spans ([2909b4e](https://github.com/future-agi/traceAI/commit/2909b4e1882eaecb74f4c3ff60b85e8cf8a63f6d))

## [0.1.0]
### Feature
- Added support for AWS Strands Agents instrumentation.
