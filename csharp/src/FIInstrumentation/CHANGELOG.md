# Changelog

## Unreleased

### Changed
- `TraceAI.Register` refuses configurations that would send Future AGI keys insecurely, and throws `FiKeyRefusedException` (a stable `Code`; the message names the setting to change and no value) before any key is attached. The rules, decided on the endpoint as written and the host the transport connects to: a Future AGI host (`*.futureagi.com`) is TLS only, so plaintext http to it is always refused, `FI_ALLOW_INSECURE_HTTP` and `RegisterOptions.AllowInsecureHttp` never apply to it, and a gRPC endpoint on it is made https; plaintext to a literal loopback host (`localhost`, `127.0.0.1`, `::1`) needs no setting; plaintext to any other host is refused unless `FI_ALLOW_INSECURE_HTTP` is exactly `1` or `RegisterOptions.AllowInsecureHttp` is set, which warns once per process naming the setting and the collector's `scheme://host:port`. An endpoint whose host a second parser reads differently (a backslash before an `@`, `%5C`, an `@` after the host part, an empty host) is refused. `FiKeyTransport.Decide` is that decision.
- A request that carries the keys never follows a redirect, and a plaintext one (loopback or opted in) is never sent through a proxy; an https request keeps the normal proxy behaviour. The export client keeps the exporter's timeout (`OTEL_EXPORTER_OTLP_TIMEOUT`, default 10 seconds).

## [1.0.1](https://github.com/future-agi/traceAI/compare/nuget-fi-instrumentation-otel-v1.0.0...nuget-fi-instrumentation-otel-v1.0.1) (2026-11-05)


### Bug Fixes

* **fi-instrumentation-otel:** enforce tracing privacy controls ([#366](https://github.com/future-agi/traceAI/issues/366)) ([d48fe33](https://github.com/future-agi/traceAI/commit/d48fe3317b10daa8dc66b0344a8018811a7a68e8))
