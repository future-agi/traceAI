// Source-only preload: no context manager, tracer provider or propagator is
// registered. Give the application access to the launcher so it can flush before
// reporting, and let Node load the TypeScript sources.
const { resolve } = require("node:path");
require("ts-node").register({
  project: resolve(__dirname, "../../tsconfig.json"),
  transpileOnly: true,
});
const { context } = require("@opentelemetry/api");
globalThis.__mistralNoOtelTest = {
  contextManager: () => context._getContextManager().constructor.name,
  auto: require("../../../traceai_auto/src/index.ts"),
};
