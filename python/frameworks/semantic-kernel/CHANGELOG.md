# Changelog

## 0.1.0

- Initial release. `SemanticKernelInstrumentor().instrument()` enables Semantic Kernel's native OpenTelemetry GenAI diagnostics in process and installs a mapping-only span processor (span kind, provider alias, total tokens, session, content default). No `wrapt`, no wrapping of `Kernel.invoke`.
- Supports `semantic-kernel` 1.38.0 to 1.x; tested on 1.38.0 and 1.44.1 with Python 3.10 to 3.13, and with `opentelemetry-sdk` 1.29.0 (floor) and 1.45.0.
- An `execute_tool` span is `TOOL` when it carries a tool call id or runs directly under `AutoFunctionInvocationLoop`, so connectors that send no tool call id (Ollama) still show tools.
- `instrument()` logs a WARNING and skips a Semantic Kernel module it cannot import, and logs a WARNING and changes nothing for a non-SDK tracer provider, instead of raising.
- `instrument()` installs and owns its own processor; a `SemanticKernelSpanProcessor` you installed is left in place by both `instrument()` and `uninstrument()`.
- `instrument(config=TraceConfig(...))`, `FI_HIDE_INPUTS` and `FI_HIDE_OUTPUTS` drop the input / output content keys that `sensitive=True` would keep.
