# Changelog

## Unreleased

### Changed
- `TraceAI.Register` refuses configurations that would send Future AGI keys insecurely, and throws `FiKeyRefusedException` (a stable `Code`; the message names the setting to change and no value) before any key is attached. The rules, decided on the endpoint as written and the host the transport connects to: a Future AGI host (`*.futureagi.com`) is TLS only, so plaintext http to it is always refused, `FI_ALLOW_INSECURE_HTTP` and `RegisterOptions.AllowInsecureHttp` never apply to it, and a gRPC endpoint on it is made https; plaintext to a literal loopback host (`localhost`, `127.0.0.1`, `::1`) needs no setting; plaintext to any other host is refused unless `FI_ALLOW_INSECURE_HTTP` is exactly `1` or `RegisterOptions.AllowInsecureHttp` is set, which warns once per process naming the setting and the collector's `scheme://host:port`. An endpoint whose host a second parser reads differently is refused: any backslash or `%5C` in the authority, an `@` in the path, query or fragment, an empty host, and a host name whose `Uri.Host` and `Uri.IdnHost` differ (non-ASCII letters, full-width dots). IPv6 zone literals (`[fe80::1%25eth0]`, `[::1%25lo]`) are not loopback: they need the setting like any other plaintext target. `FiKeyTransport.Decide` is that decision.
- `TraceAI.Register` also refuses, with `FiKeyRefusedException` (`FI_AMBIENT_KEY_HEADERS`), `OTEL_EXPORTER_OTLP_HEADERS` or `OTEL_EXPORTER_OTLP_TRACES_HEADERS` that carries an `x-api-key` or `x-secret-key` header, or a value that contains one of the Future AGI keys it uses. The text is checked as written and with every percent escape resolved, as the exporter resolves it.
- A request that carries the keys never follows a redirect, and a plaintext one (loopback or opted in) is never sent through a proxy; an https request keeps the normal proxy behaviour. The export client keeps the exporter's timeout (`OTEL_EXPORTER_OTLP_TIMEOUT`, default 10 seconds).

## [1.1.0](https://github.com/future-agi/traceAI/compare/nuget-fi-instrumentation-otel-v1.0.1...nuget-fi-instrumentation-otel-v1.1.0) (2026-11-28)


### Features

* **csharp:** one decision for every key-carrying request: TLS to Future AGI, plaintext opt-in, no proxy for plaintext, no redirects, ambient headers ([b1946b3](https://github.com/future-agi/traceAI/commit/b1946b3b4f89b54af6a7c176db2f1b02a789adea))
* **csharp:** send Future AGI keys only to allowed destinations ([a42d045](https://github.com/future-agi/traceAI/commit/a42d0456321975e3f490058d0313feed595d451b))


### Bug Fixes

* **csharp:** no proxy for any plaintext key send, keep the exporter timeout, narrow the known-secret scope ([9673151](https://github.com/future-agi/traceAI/commit/967315165a26bdfc2046d33f3ffea260b8d96032))
* **csharp:** screen ambient exporter headers on the unescaped text and at Register, refuse hosts whose Host and IdnHost differ, keep registered secrets in the shared state in recency order, exact messages and CHANGELOG ([61e8a7c](https://github.com/future-agi/traceAI/commit/61e8a7cb965061d78b4134e2a98e82aa4f40b39a))

## [1.0.1](https://github.com/future-agi/traceAI/compare/nuget-fi-instrumentation-otel-v1.0.0...nuget-fi-instrumentation-otel-v1.0.1) (2026-11-05)


### Bug Fixes

* **fi-instrumentation-otel:** enforce tracing privacy controls ([#366](https://github.com/future-agi/traceAI/issues/366)) ([d48fe33](https://github.com/future-agi/traceAI/commit/d48fe3317b10daa8dc66b0344a8018811a7a68e8))
