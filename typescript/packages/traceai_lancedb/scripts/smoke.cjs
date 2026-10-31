// Run after build in a fresh process: Jest does not exercise require-in-the-middle.
const assert = require("node:assert/strict");
const { mkdtemp, rm } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const { context } = require("@opentelemetry/api");
const { AsyncLocalStorageContextManager } = require("@opentelemetry/context-async-hooks");
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = require("@opentelemetry/sdk-trace-base");
const { LanceDBInstrumentation, isPatched } = require("../dist/src/index.js");

async function main() {
  context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new LanceDBInstrumentation();
  instrumentation.setTracerProvider(provider);
  const esm = process.argv[2] === "esm";
  const lance = esm ? (await import("@lancedb/lancedb")).default : require("@lancedb/lancedb");
  if (esm) instrumentation.manuallyInstrument(lance);
  assert.equal(isPatched(), true);
  const dir = await mkdtemp(join(tmpdir(), "traceai-lancedb-smoke-"));
  let db;
  try {
    db = await lance.connect(dir);
    const table = await db.createTable("vectors", [{ id: 1, vector: [1, 0] }]);
    exporter.reset();
    assert.equal(await table.countRows(), 1);
    assert.equal((await table.search([1, 0]).limit(1).toArray()).length, 1);
    assert.deepEqual(exporter.getFinishedSpans().map(span => span.name), ["lancedb countRows", "lancedb search"]);
    instrumentation.disable();
    exporter.reset();
    await table.countRows();
    assert.equal(exporter.getFinishedSpans().length, 0);
    instrumentation.enable();
    await table.countRows();
    assert.equal(exporter.getFinishedSpans().length, 1);
    console.log(`${esm ? "ESM default/manual" : "CJS automatic"} smoke passed`);
  } finally {
    instrumentation.disable();
    db?.close();
    await provider.shutdown();
    context.disable();
    await rm(dir, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
