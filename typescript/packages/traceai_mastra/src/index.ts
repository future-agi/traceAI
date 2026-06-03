// Mastra v1 (>= 1.16) — recommended. A custom observability exporter that maps
// Mastra spans to Future AGI's gen_ai conventions (span kind + input/output) and
// ships OTLP to the collector. Depends only on the @mastra/* peers — no legacy
// OpenTelemetry/Vercel deps are loaded.
export {
  createFIObservability,
  createFIMastraExporter,
} from "./FIObservability.js";
export type {
  FIObservabilityOptions,
  FIMastraExporterOptions,
} from "./FIObservability.js";
export {
  FIMastraSpanExporter,
  type FISpanExporterConfig,
} from "./FIMastraSpanExporter.js";

// The legacy Mastra v0.x exporter (`telemetry:` config) lives at
// `@traceai/mastra/legacy` so v1 users don't pull in @traceai/vercel et al.
