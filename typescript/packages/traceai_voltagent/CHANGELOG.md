# @traceai/voltagent

## 0.1.0

- Initial release: `FIVoltAgentSpanProcessor` for `ObservabilityConfig.spanProcessors` in `@voltagent/core` 2.x. Maps VoltAgent span kinds, model, usage, session and tool keys onto Future AGI / GenAI keys on an exported copy, keeps promoted token keys on model-call spans only, drops content unless `captureContent: true`, and batches through the `register()` provider's exporter.
