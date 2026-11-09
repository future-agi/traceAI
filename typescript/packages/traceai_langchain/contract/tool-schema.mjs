// Real ChatOpenAI adapter against localhost, using the packed CJS/ESM exports.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const mode = process.argv[2] ?? "manual-cjs";
const privacy = process.argv[3] ?? "public";
const load = mode === "manual-esm" ? (name) => import(name) : async (name) => require(name);
if (privacy === "environment") process.env.FI_HIDE_INPUTS = "true";
const { LangChainInstrumentation } = await load("@traceai/langchain");
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = await load("@opentelemetry/sdk-trace-base");
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
const instrumentation = new LangChainInstrumentation({
  tracerProvider: provider,
  traceConfig: privacy === "options" ? { hideInputs: true } : undefined,
});
if (mode !== "automatic") {
  instrumentation.manuallyInstrument(await load("@langchain/core/callbacks/manager"), await load("@langchain/core/language_models/chat_models"));
}
const { ChatOpenAI } = await load("@langchain/openai");
const { awaitAllCallbacks } = await load("@langchain/core/callbacks/promises");
const secrets = ["synthetic-private-tool-description", "synthetic-private-enum-value"];
const tools = [{ type: "function", function: {
  name: "lookup",
  description: secrets[0],
  parameters: { type: "object", properties: { category: { type: "string", enum: [secrets[1]] } }, required: ["category"] },
} }];
let request;
const server = createServer(async (req, res) => {
  let body = "";
  for await (const part of req) body += part;
  request = JSON.parse(body);
  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({
    id: "chatcmpl-synthetic", object: "chat.completion", created: 1, model: "gpt-4o-mini",
    choices: [{ index: 0, message: { role: "assistant", content: "answer" }, finish_reason: "stop" }],
    usage: { prompt_tokens: 12, completion_tokens: 3, total_tokens: 15 },
  }));
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
try {
  const model = new ChatOpenAI({
    apiKey: "synthetic-not-a-provider-key", model: "gpt-4o-mini", temperature: 0, maxRetries: 0,
    configuration: { baseURL: `http://127.0.0.1:${server.address().port}/v1` },
  });
  const response = await model.bindTools(tools).invoke("query");
  assert.equal(response.content, "answer");
  assert.deepEqual(request.tools, tools, "tracing must not change provider inputs");
  await awaitAllCallbacks();
  await provider.forceFlush();
  const spans = exporter.getFinishedSpans();
  const span = spans.find((s) => s.attributes["gen_ai.request.model"] === "gpt-4o-mini");
  assert.ok(span, "retain separately recorded model metadata");
  assert.equal(span.attributes["gen_ai.usage.input_tokens"], 12);
  assert.equal(span.attributes["gen_ai.usage.output_tokens"], 3);
  assert.equal(span.attributes["gen_ai.usage.total_tokens"], 15);
  const copies = ["gen_ai.request.parameters", "gen_ai.tool.definitions"];
  // Check BOTH copies independently, with a public control that proves both exist.
  const checks = copies.map((key) => ({ key, pass: secrets.every((secret) =>
    JSON.stringify(span.attributes[key] ?? null).includes(secret) === (privacy === "public")) }));
  console.log(JSON.stringify({ core: require("@langchain/core/package.json").version, mode, privacy, checks }));
  assert.ok(checks.every((check) => check.pass), "tool schema copies must both follow input privacy");
  if (privacy !== "public") {
    const exported = JSON.stringify(spans.map((s) => ({ attributes: s.attributes, events: s.events, status: s.status })));
    for (const secret of secrets) assert.ok(!exported.includes(secret), "no other exported schema copies");
  }
} finally {
  instrumentation.disable();
  await provider.shutdown();
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
}
