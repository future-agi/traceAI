## Unreleased

- With `hide_inputs`, `hide_input_messages` or `hide_input_text`, the whole-response `output.value` is built from response-side fields (generated text, finish reason, token counts, choices, usage) while any input hide setting is on, so `details.prefill` is not recorded.

## [0.1.1] - 2026-11-12

### Changed
- traceai-huggingface now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

