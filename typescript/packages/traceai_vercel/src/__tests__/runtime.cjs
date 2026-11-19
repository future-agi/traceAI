// Standalone real-Node probes: run after `pnpm --filter @traceai/vercel build`.
// One process per version/mode intentionally tests fresh module-hook registration.
const assert = require("node:assert/strict");
const { mkdirSync, realpathSync, symlinkSync, writeFileSync, existsSync } = require("node:fs");
const { join, dirname } = require("node:path");
const { createRequire, register } = require("node:module");
const { pathToFileURL } = require("node:url");
const { trace, context } = require("@opentelemetry/api");
const { AsyncLocalStorageContextManager } = require("@opentelemetry/context-async-hooks");
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } = require("@opentelemetry/sdk-trace-base");

async function main() {
  const major = Number(process.argv[2]);
  const mode = process.argv[3];
  assert.ok([5, 6, 7].includes(major));
  assert.ok(["cjs", "esm"].includes(mode));
  const root = join(__dirname, "../..");
  const alias = major === 5 ? "ai-v5" : major === 7 ? "ai-v7" : "ai";
  const target = dirname(realpathSync(require.resolve(`${alias}/package.json`)));
  const app = join(root, "node_modules/.cache/runtime", `${major}-${mode}`);
  mkdirSync(join(app, "node_modules"), { recursive: true });
  const link = join(app, "node_modules/ai");
  if (!existsSync(link)) symlinkSync(target, link, "dir");
  const appRequire = createRequire(join(app, "app.cjs"));
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider();
  provider.addSpanProcessor(new SimpleSpanProcessor(exporter));
  const manager = new AsyncLocalStorageContextManager().enable();
  context.setGlobalContextManager(manager);
  trace.setGlobalTracerProvider(provider);
  const { enable, enableVercelInstrumentation } = mode === "esm"
    ? await import(pathToFileURL(join(root, "dist/esm/index.js")).href)
    : require("../../dist/src");
  const instrumentor = enable();
  assert.equal(instrumentor, enableVercelInstrumentation());
  assert.ok(instrumentor);

  let sdk, mocks;
  if (mode === "esm") {
    register(require.resolve("@opentelemetry/instrumentation/hook.mjs"), pathToFileURL(__filename), { data: { include: ["ai"] } });
    const entry = join(app, "app.mjs");
    writeFileSync(entry, 'export * as sdk from "ai"; export * as mocks from "ai/test";\n');
    ({ sdk, mocks } = await import(pathToFileURL(entry).href));
  } else {
    sdk = appRequire("ai");
    mocks = appRequire("ai/test");
  }
  const Model = mocks[`MockLanguageModelV${major === 5 ? 2 : major === 6 ? 3 : 4}`];
  const result = await sdk.generateText({ prompt: "hello", model: new Model({ doGenerate: {
    content: [{ type: "text", text: "automatic" }], warnings: [],
    finishReason: major === 5 ? "stop" : { unified: "stop", raw: "stop" },
    usage: major === 5 ? { inputTokens: 1, outputTokens: 2 } : {
      inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 },
      outputTokens: { total: 2, text: 2, reasoning: 0 },
    },
  } }) });
  assert.equal(result.text, "automatic");
  const modelSpans = exporter.getFinishedSpans().filter(span => span.attributes["gen_ai.span.kind"] === "LLM");
  assert.equal(modelSpans.length, 1, JSON.stringify(exporter.getFinishedSpans().map(span => [span.name, span.attributes])));
  assert.equal(modelSpans[0].attributes["output.value"], "automatic");
  await provider.shutdown();
  manager.disable();
  console.log(`PASS real Node lazy hook: AI SDK v${major} ${mode} (${modelSpans.length} model span)`);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
