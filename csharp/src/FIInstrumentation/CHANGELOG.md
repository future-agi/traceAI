# Changelog

## Unreleased

### Changed
- `TraceAI.Register` decides where the Future AGI keys may go before it attaches them: plaintext http to a Future AGI host (`*.futureagi.com`) is refused unless `FI_ALLOW_INSECURE_HTTP=1`, and the option never applies to a Future AGI host; a gRPC endpoint on a Future AGI host always uses TLS; plaintext to a literal loopback host (`localhost`, `127.0.0.1`, `::1`) needs no setting; a self-hosted collector without TLS needs `FI_ALLOW_INSECURE_HTTP=1` (exactly `1`) or `RegisterOptions.AllowInsecureHttp`, with one warning per process naming the setting and the collector's `scheme://host:port`. An endpoint whose host a second parser reads differently (a backslash before an `@`, `%5C`, an `@` after the host part, an empty host) is refused. `FiKeyTransport.Decide` is that decision; its refusals are `FiKeyRefusedException` with a stable `Code` and a message that names no value.
- Key-carrying requests do not follow redirects, and a plaintext request sent under the opt-in does not use a proxy. An https request keeps the normal proxy behaviour.

## [1.0.1](https://github.com/future-agi/traceAI/compare/nuget-fi-instrumentation-otel-v1.0.0...nuget-fi-instrumentation-otel-v1.0.1) (2026-11-05)


### Bug Fixes

* **fi-instrumentation-otel:** enforce tracing privacy controls ([#366](https://github.com/future-agi/traceAI/issues/366)) ([d48fe33](https://github.com/future-agi/traceAI/commit/d48fe3317b10daa8dc66b0344a8018811a7a68e8))
