# Changelog

## [0.1.1] - pending release
### Changed
- A privacy switch now accepts every on and off value, and an unrecognised value hides. The `FI_HIDE_*` variables that the span processor and `enable()` read accept `true`, `1`, `yes`, `on` and `false`, `0`, `no`, `off`, in any case and with surrounding spaces ignored. Any other non-empty value turns the switch on, with one warning per variable through the OpenTelemetry diagnostic logger that never repeats the value. Before, only `true` turned a switch on, so this hides more and never less. An empty variable now counts as unset instead of off, so the default applies: where the removed `captureContent: false` option is still passed, an empty `FI_HIDE_INPUTS` or `FI_HIDE_OUTPUTS` now keeps inputs or outputs hidden. An option in `traceConfig` still wins over the variable.

### Fix
- The span processor marks only Genkit spans as first-party copies. An application span that reaches it through the shared provider keeps the credential name rule for its session and user ids.

### Feature
- `enable()` attaches traceAI export to Genkit for the zero-code agent `@traceai/auto`. It uses only the traceAI export branch, so the application's other exporters keep Genkit's native spans.
- Automatic exports put Genkit generation config in `gen_ai.request.parameters`, governed by `hideLLMInvocationParameters` / `FI_HIDE_LLM_INVOCATION_PARAMETERS`. A truncated request that cannot be split safely is redacted.

### Fix
- SDK value-limit evidence is kept before mapping, so truncation is still reported after scrubbing.
- In `enable()` mode, exports remove the echoed `request` field from JSON object outputs of Genkit `generate` util spans. If parsing the output fails, it is redacted and `output.mime_type` is removed.

