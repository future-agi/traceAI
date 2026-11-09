## [0.2.0] - 2026-06-03
### Feature
- Support Mastra v1 observability: `createFIObservability` / `createFIMastraExporter` wire Future AGI into the new `@mastra/observability` pipeline (Mastra v1 removed the `telemetry:` config the previous exporter relied on).
- Map Mastra spans to Future AGI's `gen_ai.*` conventions — span kind (`FISpanKind`) and `input.value` / `output.value` enrichment — so traces render fully in the Future AGI UI.
- Move the legacy v0.x `FITraceExporter` integration to the `@traceai/mastra/legacy` subpath.

## [0.2.2](https://github.com/future-agi/traceAI/compare/npm-traceai-mastra-v0.2.1...npm-traceai-mastra-v0.2.2) (2026-11-05)


### Bug Fixes

* **mastra:** enforce privacy and restore SDK compatibility ([#567](https://github.com/future-agi/traceAI/issues/567)) ([b4b0b8f](https://github.com/future-agi/traceAI/commit/b4b0b8feabd2ffd045e84a25210a613c5c972c68))

## [0.2.1](https://github.com/future-agi/traceAI/compare/npm-traceai-mastra-v0.2.0...npm-traceai-mastra-v0.2.1) (2026-11-01)


### Bug Fixes

* **npm:** report the real package version in instrumentation metadata ([9fa1ee0](https://github.com/future-agi/traceAI/commit/9fa1ee02e59618c0f68d63c8959954ff00e59839))

## [0.1.0]
### Feature
- Initial release: `FITraceExporter` for Mastra v0.x (`telemetry:` config).
