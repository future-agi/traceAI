# Changelog

## 0.1.0

- Initial release. `SemanticKernelInstrumentor().instrument()` enables Semantic Kernel's native OpenTelemetry GenAI diagnostics in process and installs a mapping-only span processor (span kind, provider alias, total tokens, session, content default). No `wrapt`, no wrapping of `Kernel.invoke`.
- Supports `semantic-kernel` 1.38.0 to 1.x; tested on 1.38.0 and 1.44.1.
