## [0.1.0]

### Added
- Export Cognee's native OpenTelemetry spans to Future AGI without duplicate instrumentation.
- Map native search and structured-output model calls to Future AGI span types while preserving IDs, parents and error statuses.
- Exclude content, error descriptions and stack traces by default; provide content opt-in with bounded structural credential-field redaction.
- Reuse a provider-owned processor across configurations, keep other exporters unchanged, and isolate spans across handle shutdown and reconfiguration.
- Require `fi-instrumentation-otel>=1.2.2,<2` and Python 3.10–3.13.

### Fixed
- Drain ended spans and close the owned exporter during interpreter exit, while keeping normal handle shutdown nonblocking and other exporters unchanged.
