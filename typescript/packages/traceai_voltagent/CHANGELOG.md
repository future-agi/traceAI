# @traceai/voltagent

## 0.1.0

- Initial release: `FIVoltAgentSpanProcessor` for `ObservabilityConfig.spanProcessors` in `@voltagent/core` 2.x. Maps VoltAgent span kinds, model, usage, session and tool keys onto Future AGI / GenAI keys on an exported copy, keeps promoted token keys on model-call spans only, and batches through the `register()` provider's exporter.
- Privacy follows the traceAI `TraceConfig` convention: content is exported by default and hidden with `traceConfig` or the `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` / `FI_HIDE_INPUT_MESSAGES` / `FI_HIDE_OUTPUT_MESSAGES` / `FI_HIDE_INPUT_TEXT` / `FI_HIDE_OUTPUT_TEXT` env vars (the earlier `captureContent` option is gone). Credentials, stack traces (`error.stack`, `exception.stacktrace`, frames inside JSON) and local paths are never exported; error text also loses emails and the machine's user and host names, and is dropped when inputs or outputs are hidden.
