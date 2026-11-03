const assert = require("node:assert/strict");
const { createRequire } = require("node:module");
const { dirname, resolve } = require("node:path");
const { pathToFileURL } = require("node:url");
const {
  InMemorySpanExporter,
  SimpleSpanProcessor,
} = require("@opentelemetry/sdk-trace-base");
const { NodeTracerProvider } = require("@opentelemetry/sdk-trace-node");

(async () => {
  const mode = process.argv[2];
  const modulePath = resolve(
    __dirname,
    "../..",
    "dist",
    mode === "esm" ? "esm" : "cjs",
    "index.js",
  );
  const { CerebrasInstrumentation, isPatched } =
    mode === "esm"
      ? await import(pathToFileURL(modulePath).href)
      : require(modulePath);
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  const instrumentation = new CerebrasInstrumentation();
  instrumentation.setTracerProvider(provider);
  const sdkPath =
    process.env.CEREBRAS_SDK_PATH ||
    dirname(require.resolve("@cerebras/cerebras_cloud_sdk/package.json"));
  const sdkRequire = createRequire(resolve(sdkPath, "package.json"));
  const sdk =
    mode === "esm"
      ? await import(pathToFileURL(resolve(sdkPath, "index.mjs")).href)
      : sdkRequire("@cerebras/cerebras_cloud_sdk");
  if (mode === "esm") instrumentation.manuallyInstrument(sdk);
  // CJS intentionally relies on the automatic require hook, not manual patching.
  assert.equal(isPatched(), true);
  const client = new sdk.Cerebras({
    apiKey: "offline-test-key",
    baseURL: "http://127.0.0.1:9",
    warmTCPConnection: false,
    maxRetries: 0,
    fetch: async () =>
      new Response(JSON.stringify({ model: "offline", choices: [] }), {
        headers: { "content-type": "application/json" },
      }),
  });
  try {
    await client.chat.completions.create({
      model: "offline",
      messages: [{ role: "user", content: "hello" }],
    });
    assert.equal(exporter.getFinishedSpans().length, 1);
    instrumentation.disable();
    assert.equal(isPatched(), false);
    await client.chat.completions.create({ model: "offline", messages: [] });
    assert.equal(exporter.getFinishedSpans().length, 1);
    instrumentation.enable();
    await client.chat.completions.create({ model: "offline", messages: [] });
    assert.equal(exporter.getFinishedSpans().length, 2);
    console.log(
      `${mode}: automatic/manual import, disable and re-enable passed`,
    );
  } finally {
    instrumentation.disable();
    await provider.shutdown();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
