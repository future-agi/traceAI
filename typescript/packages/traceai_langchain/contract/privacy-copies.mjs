// Real adapter regression for opaque prompt/function-call copies, not mocked tracing.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const mode = process.argv[2] ?? "manual-cjs";
const privacy = process.argv[3] ?? "public";
const flag = process.argv[4] ?? "combined";
const flags = flag === "combined" ? ["hideInputs", "hideOutputs"] : [flag];
const envNames = {
  hideInputs: "FI_HIDE_INPUTS", hideInputText: "FI_HIDE_INPUT_TEXT",
  hideInputImages: "FI_HIDE_INPUT_IMAGES", hideInputMessages: "FI_HIDE_INPUT_MESSAGES",
  hideOutputs: "FI_HIDE_OUTPUTS", hideOutputText: "FI_HIDE_OUTPUT_TEXT",
  hideOutputMessages: "FI_HIDE_OUTPUT_MESSAGES",
};
assert.ok(flags.every(name => name in envNames));
assert.ok(["public", "options", "environment"].includes(privacy));
assert.ok(["manual-cjs", "manual-esm", "automatic"].includes(mode));
for (const name of Object.values(envNames)) delete process.env[name];
if (privacy === "environment") for (const name of flags) process.env[envNames[name]] = "true";
const hidden = privacy !== "public";
const hidePrompt = hidden && flags.some(name => name.startsWith("hideInput"));
const hideFunction = hidden && flags.some(name => name.startsWith("hideOutput"));
const load = mode === "manual-esm" ? name => import(name) : async name => require(name);
const { LangChainInstrumentation } = await load("@traceai/langchain");
const { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } = await load("@opentelemetry/sdk-trace-base");
const exporter = new InMemorySpanExporter();
const started = new Set(), ended = new Map();
const provider = new BasicTracerProvider({ spanProcessors: [
  { onStart(span) { started.add(span.spanContext().spanId); },
    onEnd(span) { const id = span.spanContext().spanId; ended.set(id, (ended.get(id) ?? 0) + 1); },
    async forceFlush() {}, async shutdown() {} },
  new SimpleSpanProcessor(exporter),
] });
const instrumentation = new LangChainInstrumentation({
  tracerProvider: provider,
  traceConfig: privacy === "options" ? Object.fromEntries(flags.map(name => [name, true])) : undefined,
});
if (mode !== "automatic") instrumentation.manuallyInstrument(
  await load("@langchain/core/callbacks/manager"),
  await load("@langchain/core/language_models/chat_models"),
  await load("@langchain/core/runnables"),
);
const { ChatOpenAI } = await load("@langchain/openai");
const { ChatPromptTemplate } = await load("@langchain/core/prompts");
const { awaitAllCallbacks } = await load("@langchain/core/callbacks/promises");
const system = "synthetic-private-static-system-instructions";
const argument = "synthetic-private-legacy-function-argument";
const key = "synthetic-not-a-provider-key";
const functions = [{ name: "lookup", parameters: { type: "object", properties: { secret: { type: "string" } } } }];
const functionCall = { name: "lookup", arguments: JSON.stringify({ secret: argument }) };
const requests = [], checks = [];
const server = createServer(async (req, res) => {
  let body = ""; for await (const part of req) body += part;
  const request = JSON.parse(body); requests.push(request);
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify({ id: "chatcmpl-synthetic", object: "chat.completion", created: 1, model: request.model,
    choices: [{ index: 0, message: request.functions ? { role: "assistant", content: null, function_call: functionCall } :
      { role: "assistant", content: "answer" }, finish_reason: request.functions ? "function_call" : "stop" }],
    usage: { prompt_tokens: 12, completion_tokens: 3, total_tokens: 15 },
  }));
});
const serialize = spans => JSON.stringify(spans.map(span => ({ attributes: span.attributes, events: span.events, status: span.status, links: span.links })));
async function check(name, fn) {
  try { await fn(); checks.push({ name, pass: true }); }
  catch (error) { checks.push({ name, pass: false, error: error.message }); }
}
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
try {
  const model = () => new ChatOpenAI({ apiKey: key, model: "gpt-4o-mini", maxRetries: 0,
    configuration: { baseURL: `http://127.0.0.1:${server.address().port}/v1` } });
  const result = await ChatPromptTemplate.fromMessages([["system", system], ["human", "{question}"]])
    .pipe(model().withConfig({ runName: "template-model" })).invoke({ question: "query" });
  assert.equal(result.content, "answer");
  assert.deepEqual(requests.at(-1).messages, [{ role: "system", content: system }, { role: "user", content: "query" }]);
  const legacy = await model().invoke("query", { runName: "legacy-function", functions, function_call: { name: "lookup" } });
  assert.deepEqual(legacy.additional_kwargs.function_call, functionCall, "do not redact the caller's response");
  assert.deepEqual(requests.at(-1).functions, functions, "do not mutate provider inputs");
  assert.deepEqual(requests.at(-1).function_call, { name: "lookup" });
  await awaitAllCallbacks(); await provider.forceFlush();
  const spans = exporter.getFinishedSpans();
  await check("static template copy respects input privacy", () => {
    const copies = spans.filter(span => "llm.prompt_template.template" in span.attributes);
    assert.ok(copies.length > 0, "exercise the actual template attribute");
    for (const span of copies) assert.equal(JSON.stringify(span.attributes["llm.prompt_template.template"]).includes(system), !hidePrompt);
    if (hidePrompt && hideFunction) assert.equal(serialize(spans).includes(system), false, "no other exported copies");
  });
  await check("legacy function copy respects output privacy", () => {
    const span = spans.find(span => span.name === "legacy-function"); assert.ok(span);
    assert.ok("gen_ai.tool.call" in span.attributes, "exercise the legacy function attribute");
    assert.equal(JSON.stringify(span.attributes["gen_ai.tool.call"]).includes(argument), !hideFunction);
    if (hideFunction) assert.equal(serialize([span]).includes(argument), false, "no other exported argument copies");
  });
  await check("model and usage survive masking", () => {
    for (const name of ["template-model", "legacy-function"]) {
      const matches = spans.filter(span => span.name === name); assert.equal(matches.length, 1);
      const span = matches[0]; assert.equal(span.attributes["fi.span.kind"], "LLM");
      assert.equal(span.attributes["gen_ai.request.model"], "gpt-4o-mini");
      for (const [suffix, count] of Object.entries({ input_tokens: 12, output_tokens: 3, total_tokens: 15 })) {
        assert.equal(span.attributes["gen_ai.usage." + suffix], count);
      }
    }
  });
  await check("each span ends once without exporting credentials", () => {
    assert.ok(started.size > 0); assert.equal(started.size, ended.size);
    for (const id of started) assert.equal(ended.get(id), 1);
    assert.equal(serialize(spans).includes(key), false);
  });
} finally {
  instrumentation.disable(); await provider.shutdown();
  server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
}
console.log(JSON.stringify({ core: require("@langchain/core/package.json").version,
  adapter: require("@langchain/openai/package.json").version, mode, privacy, flag, checks,
  started: started.size, ended: ended.size }));
assert.ok(checks.every(check => check.pass), "opaque content copies must follow privacy controls");
