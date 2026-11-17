// Source-only test preload for an application with no OpenTelemetry of its own. Unlike
// registry-preload.cjs it registers no context manager, tracer provider or propagator.
// It only lets the child process load the TypeScript sources, and hands the application
// the launcher's module, so that it can flush before it prints its result.
const { resolve } = require("node:path");
require("ts-node").register({
  project: resolve(__dirname, "../../tsconfig.json"),
  transpileOnly: true,
});
const { context } = require("@opentelemetry/api");
globalThis.__vercelNoOtelTest = {
  // The registered context manager's class name: NoopContextManager when there is none.
  contextManager: () => context._getContextManager().constructor.name,
  auto: require("../../../traceai_auto/src/index.ts"),
};
