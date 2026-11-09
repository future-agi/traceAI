# Changelog

## [0.1.1] - pending release
### Feature
- `enable()` attaches traceAI export to Genkit for the zero-code agent `@traceai/auto`. It uses only the traceAI export branch, so the application's other exporters keep Genkit's native spans.
- Automatic exports put Genkit generation config in `gen_ai.request.parameters`, governed by `hideLLMInvocationParameters` / `FI_HIDE_LLM_INVOCATION_PARAMETERS`. A truncated request that cannot be split safely is redacted.

### Fix
- SDK value-limit evidence is kept before mapping, so truncation is still reported after scrubbing.
- In `enable()` mode, exports remove the echoed `request` field from JSON object outputs of Genkit `generate` util spans. If parsing the output fails, it is redacted and `output.mime_type` is removed.

