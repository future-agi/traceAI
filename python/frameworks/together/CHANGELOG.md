# Changelog

## Unreleased

- With `hide_inputs`, `hide_input_messages` or `hide_input_text`, the whole-response `output.value` is built from response-side fields (id, model, choices text/message, finish reason, usage) while any input hide setting is on, so a request echo such as `prompt` is not recorded.

## [0.1.2] - 2026-11-12

### Changed
- traceai-together now requires fi-instrumentation-otel 1.0.0 or later. Action: Upgrade fi-instrumentation-otel if your environment pins an earlier version.

## [0.1.1] - 2026-10-29

### Fixed
- Publish package changes merged after 0.1.0 was uploaded to PyPI; one-time bootstrap patch for the automated release pipeline.
