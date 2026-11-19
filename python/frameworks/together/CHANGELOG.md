# Changelog

## [0.1.3] - 2026-11-19

- With `hide_inputs`, `hide_input_messages`, `hide_input_text` or `hide_input_images`, the whole-response `output.value` is built from response-side fields (id, model, choices text/message, finish reason, usage) while any input hide setting is on, so a request echo such as `prompt` is not recorded.
- The 3.x resource classes (`CompletionsResource`, `EmbeddingsResource` and their async versions) are instrumented alongside the 1.x ones. Streamed calls return a proxy that keeps the SDK stream's iteration, context-manager and close behaviour and its attributes, and ends the span once on exhaustion, error, close or early break (marked `together.stream.incomplete`).

## [0.1.2] - 2026-11-12

### Changed
- traceai-together now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.
