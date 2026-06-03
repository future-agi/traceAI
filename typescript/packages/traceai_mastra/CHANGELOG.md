## [0.2.0] - 2026-06-03
### Feature
- Support Mastra v1 observability: `createFIObservability` / `createFIMastraExporter` wire Future AGI into the new `@mastra/observability` pipeline (Mastra v1 removed the `telemetry:` config the previous exporter relied on).
- Map Mastra spans to Future AGI's `gen_ai.*` conventions — span kind (`FISpanKind`) and `input.value` / `output.value` enrichment — so traces render fully in the Future AGI UI.
- Move the legacy v0.x `FITraceExporter` integration to the `@traceai/mastra/legacy` subpath.

## [0.1.0]
### Feature
- Initial release: `FITraceExporter` for Mastra v0.x (`telemetry:` config).
