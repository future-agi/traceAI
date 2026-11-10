const assert = require("node:assert/strict");
const { createRequire } = require("node:module");
const { VLLMInstrumentation, isPatched } = require("../dist/cjs");
const coreRequire = createRequire(require.resolve("@traceai/fi-core"));
const { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } =
  coreRequire("@opentelemetry/sdk-trace-base");
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({
  spanProcessors: [new SimpleSpanProcessor(exporter)],
});
const instrumentation = new VLLMInstrumentation({
  baseUrlPattern: "vllm.test",
});
instrumentation.setTracerProvider(provider);
// Load the actual SDK only after installing the CommonJS require hook.
const { OpenAI } = require(process.env.OPENAI_MODULE || "openai");
(async () => {
  assert.equal(isPatched(), true);
  const client = new OpenAI({
    apiKey: "synthetic",
    baseURL: "http://vllm.test/v1",
    fetch: async () =>
      new Response(
        JSON.stringify({
          id: "id",
          model: "model",
          choices: [
            {
              index: 0,
              message: { role: "assistant", content: "ok" },
              finish_reason: "stop",
            },
          ],
        }),
        { headers: { "content-type": "application/json" } },
      ),
  });
  await client.chat.completions.create({
    model: "model",
    messages: [{ role: "user", content: "hello" }],
  });
  assert.equal(exporter.getFinishedSpans().length, 1);
  assert.equal(
    exporter.getFinishedSpans()[0].attributes["gen_ai.provider.name"],
    "vllm",
  );
  instrumentation.disable();
  assert.equal(isPatched(), false);
  await provider.shutdown();
  console.log("Automatic CommonJS resource instrumentation passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
