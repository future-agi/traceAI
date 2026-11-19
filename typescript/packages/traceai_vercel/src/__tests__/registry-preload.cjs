// Source-only test preload: exercise the real zero-code entry without stale dist
// files or rebuilding packages owned by other workers.
const { resolve } = require("node:path");
require("ts-node").register({
  project: resolve(__dirname, "../../tsconfig.json"),
  transpileOnly: true,
});
const { context, trace, SpanStatusCode, createTraceState } = require("@opentelemetry/api");
const { AsyncLocalStorageContextManager } = require("@opentelemetry/context-async-hooks");
const { BasicTracerProvider, SimpleSpanProcessor } = require("@opentelemetry/sdk-trace-base");
const { createRequire } = require("node:module");
const { Resource } = createRequire(require.resolve("@opentelemetry/sdk-trace-base"))("@opentelemetry/resources");
const makeSecret = intermediate => `runtime-vercel-v${process.argv[2]}-${process.argv[3]}-${intermediate}-private+/credential-12345`;
const raw = [], live = new Set();
const provider = new BasicTracerProvider({ resource: new Resource({ echoed: makeSecret(false) }) });
provider.addSpanProcessor(new SimpleSpanProcessor({
  export(spans, done) { raw.push(...spans); done({ code: 0 }); },
  async shutdown() {},
}));
provider.addSpanProcessor({
  onStart(span) { live.add(span.spanContext().spanId); },
  onEnd(span) { live.delete(span.spanContext().spanId); },
  async forceFlush() {}, async shutdown() {},
});
const manager = new AsyncLocalStorageContextManager().enable();
context.setGlobalContextManager(manager);
trace.setGlobalTracerProvider(provider);
globalThis.__vercelRegistryTest = {
  context, trace, SpanStatusCode, createTraceState, provider, raw, live, manager, makeSecret,
  core: require("@traceai/fi-core"),
  auto: require("../../../traceai_auto/src/index.ts"),
};
