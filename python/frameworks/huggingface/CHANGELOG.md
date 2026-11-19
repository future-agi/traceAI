## [0.1.2] - 2026-11-19

- With `hide_inputs`, `hide_input_messages`, `hide_input_text` or `hide_input_images`, the whole-response `output.value` is built from response-side fields (generated text, finish reason, token counts, choices, usage) while any input hide setting is on, so `details.prefill` is not recorded.
- Input token counts are kept under the input hide settings; the prefill tokens themselves are not.
- Requires Python 3.10 or later.

## [0.1.1] - 2026-11-12

### Changed
- traceai-huggingface now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

