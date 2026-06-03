// Legacy Mastra v0.x integration (the old `telemetry:` config). Deprecated and
// non-functional on Mastra v1, where the `telemetry` key was removed. Kept on a
// separate subpath (`@traceai/mastra/legacy`) so it doesn't burden v1 users with
// the @traceai/vercel / fi-semantic-conventions dependencies.
//
// For Mastra v1 use `createFIObservability` from the package root instead.
export * from "./FITraceExporter.js";
export { isFISpan } from "./utils.js";
