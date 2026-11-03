// Run outside Jest to exercise the actual CJS require hook and native ESM loader.
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { createRequire } = require("node:module");
const {
  BasicTracerProvider,
  SimpleSpanProcessor,
  InMemorySpanExporter,
} = require("@opentelemetry/sdk-trace-base");
const { WeaviateInstrumentation, isPatched } = require("../../dist/src");

async function main() {
  const mode = process.argv[2];
  assert.ok(["cjs", "esm"].includes(mode));
  const entry = require.resolve(
    process.env.WEAVIATE_TEST_PACKAGE || "weaviate-client",
  );
  const sdkRequire = createRequire(entry);
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  const instrumentation = new WeaviateInstrumentation();
  instrumentation.setTracerProvider(provider);
  let sdk, connection;
  if (mode === "esm") {
    const esmDir = path.resolve(path.dirname(entry), "../esm");
    sdk = await import(pathToFileURL(path.join(esmDir, "index.js")));
    connection = await import(
      pathToFileURL(path.join(esmDir, "connection/index.js"))
    );
    // A namespace has read-only named exports; the supported default object is mutable.
    instrumentation.manuallyInstrument(sdk);
  } else {
    // Deliberately do NOT call manuallyInstrument: exercise the installed OTel hook.
    sdk = sdkRequire("weaviate-client");
    connection = sdkRequire("./connection/index.js");
  }
  assert.equal(isPatched(), true);
  const originalUse = connection.ConnectionGRPC.use;
  let writes = 0;
  connection.ConnectionGRPC.use = async () => ({
    connection: {
      http: {},
      close() {},
      getDetails: () => ({}),
      postReturn: async () => {
        writes++;
        return { id: "00000000-0000-0000-0000-000000000001" };
      },
      search: async () => ({ withFetch: async () => ({ results: [] }) }),
    },
    dbVersionProvider: {},
    dbVersionSupport: new Proxy(
      { getVersion: async () => "1.35.0" },
      {
        get: (target, key) => target[key] || (async () => ({ supports: true })),
      },
    ),
  });
  try {
    const client = await sdk.default.connectToLocal({ skipInitChecks: true });
    const collection = client.collections
      .get("Articles")
      .withTenant("offline")
      .withConsistency("QUORUM");
    await collection.data.insert({ title: "offline document" });
    await collection.query.fetchObjects({ limit: 1 });
    assert.equal(writes, 1);
    assert.deepEqual(
      exporter.getFinishedSpans().map((s) => s.name),
      ["weaviate insert", "weaviate fetchObjects"],
    );
    instrumentation.disable();
    assert.equal(isPatched(), false);
    await collection.query.fetchObjects();
    assert.equal(exporter.getFinishedSpans().length, 2);
    await client.close();
    console.log(JSON.stringify({ mode, spans: 2, writes, disabled: true }));
  } finally {
    instrumentation.disable();
    connection.ConnectionGRPC.use = originalUse;
    await provider.shutdown();
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
