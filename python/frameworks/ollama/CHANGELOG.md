## Unreleased

- With `hide_inputs`, `hide_input_messages`, `hide_input_text` or `hide_input_images`, the whole-response `output.value` is built from response-side fields (message, response, done reason, durations, counts) while any input hide setting is on, so `context` token ids are not recorded.

## [0.1.1] - 2026-11-12

### Changed
- traceai-ollama now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

