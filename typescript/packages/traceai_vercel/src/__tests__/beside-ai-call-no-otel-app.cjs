// Copied into a temporary application with a real `ai` package and no OpenTelemetry of
// its own: no context manager, tracer provider or propagator, no telemetry options and
// no manual instrumentors. It makes one AI SDK call, whose model sends one request to
// the provider, and one plain fetch to the same chat route while that call is open.
// The plain fetch has no AI integration of its own, so only wire capture can record it.
// The embedding calls send their model's request to the provider's embeddings route;
// their plain fetch still goes to the chat route.
const assert = require("node:assert/strict");
const sdk = require("ai");
const mocks = require("ai/test");
const { auto, contextManager } = globalThis.__vercelNoOtelTest;
const [majorText, method, mode] = process.argv.slice(2);
const major = Number(majorText), traced = mode === "traced";
const { SDK_PROMPT, PLAIN_PROMPT } = process.env;
assert(["generateText", "streamText", "embed", "embedMany", "generateObject", "streamObject"].includes(method));
assert(SDK_PROMPT && PLAIN_PROMPT);

const post = (content, stream) => fetch(process.env.MOCK_URL + "/v1/chat/completions", {
  method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ model: "fixture", messages: [{ role: "user", content }], stream }),
});
// The application's own request, with no SDK. Its answer is read to the end.
const plainFetch = () => post(PLAIN_PROMPT, false).then(response => response.json());

const usage = () => major === 5 ? { inputTokens: 3, outputTokens: 4, totalTokens: 7 } : {
  inputTokens: { total: 3, noCache: 3, cacheRead: 0, cacheWrite: 0 },
  outputTokens: { total: 4, text: 4, reasoning: 0 },
};
const finishReason = () => major === 5 ? "stop" : { unified: "stop", raw: "stop" };
let providerCalls = 0, plainFetchSent;
const afterPlainFetch = new Promise(resolve => { plainFetchSent = resolve; });
// The plain fetch for the embedding and object calls. Starting it also releases the
// model's stream, so no call can wait on a plain fetch that was never sent.
const plainFetchBeside = () => { const pending = plainFetch(); plainFetchSent(); return pending; };
// The object calls ask for this shape, and the model answers with it as JSON text.
const schema = sdk.jsonSchema({ type: "object", properties: { answer: { type: "string" } }, required: ["answer"], additionalProperties: false });
// The model's transport: the AI SDK call's own request to the provider.
const doGenerate = async () => {
  providerCalls++;
  const response = await post(SDK_PROMPT, false);
  assert.equal(response.status, 200);
  const completion = await response.json();
  const text = completion.choices[0].message.content;
  return { content: [{ type: "text", text: method === "generateObject" ? JSON.stringify({ answer: text }) : text }],
    finishReason: finishReason(), usage: usage(), warnings: [] };
};
const doStream = async () => {
  providerCalls++;
  const response = await post(SDK_PROMPT, true);
  assert.equal(response.status, 200);
  const deltas = (await response.text()).split("\n").filter(line => line.startsWith("data: {"))
    .map(line => JSON.parse(line.slice(6)).choices[0].delta.content);
  assert.equal(deltas.length, 2);
  // For streamObject the first text is already a whole partial object, so the
  // application sees one before the stream waits; the closing brace follows.
  const texts = method === "streamObject" ? ['{"answer":' + JSON.stringify(deltas.join("")), "}"] : deltas;
  let opened = false;
  return { stream: new ReadableStream({ async pull(controller) {
    if (!opened) {
      opened = true;
      controller.enqueue({ type: "stream-start", warnings: [] });
      controller.enqueue({ type: "text-start", id: "text" });
      controller.enqueue({ type: "text-delta", id: "text", delta: texts[0] });
      return;
    }
    // The model's stream stays open after its first text until the application has
    // sent its plain fetch, so that request is always sent while the stream is open.
    await afterPlainFetch;
    controller.enqueue({ type: "text-delta", id: "text", delta: texts[1] });
    controller.enqueue({ type: "text-end", id: "text" });
    controller.enqueue({ type: "finish", finishReason: finishReason(), usage: usage() });
    controller.close();
  } }) };
};
// The embedding model's transport: one request to the provider's embeddings route.
const doEmbed = async ({ values }) => {
  providerCalls++;
  const response = await fetch(process.env.MOCK_URL + "/v1/embeddings", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "fixture-embedding", input: values }),
  });
  assert.equal(response.status, 200);
  const answer = await response.json();
  return { embeddings: answer.data.map(item => item.embedding), usage: { tokens: answer.usage.prompt_tokens }, warnings: [] };
};

(async () => {
  const Model = mocks[`MockLanguageModelV${major === 5 ? 2 : major === 6 ? 3 : 4}`];
  const model = new Model({ provider: "loopback.test", modelId: "fixture", doGenerate, doStream });
  const options = { model, prompt: SDK_PROMPT, maxRetries: 0 };
  let text = "", plain;
  if (method === "generateText") {
    // The AI SDK call starts first and the plain fetch second, in the same tick.
    const [result, answer] = await Promise.all([sdk.generateText(options), plainFetch()]);
    text = result.text; plain = answer;
  } else if (method === "streamText") {
    const result = sdk.streamText(options);
    assert(!(result instanceof Promise));
    let pending;
    for await (const part of result.textStream) {
      // The plain fetch is sent at the first text, while the application reads the stream.
      if (!pending) { pending = plainFetch(); plainFetchSent(); }
      text += part;
    }
    assert(pending, "the stream gave no text");
    plain = await pending;
  } else if (method === "embed" || method === "embedMany") {
    const EmbeddingModel = mocks[`MockEmbeddingModelV${major === 5 ? 2 : major === 6 ? 3 : 4}`];
    const embeddingModel = new EmbeddingModel({ provider: "loopback.test", modelId: "fixture-embedding", maxEmbeddingsPerCall: 1, doEmbed });
    const embedOptions = method === "embed"
      ? { model: embeddingModel, value: SDK_PROMPT, maxRetries: 0 }
      : { model: embeddingModel, values: [SDK_PROMPT], maxRetries: 0 };
    // The AI SDK call starts first and the plain fetch second, in the same tick.
    const [result, answer] = await Promise.all([sdk[method](embedOptions), plainFetchBeside()]);
    text = (method === "embed" ? result.embedding : result.embeddings[0]).join(" "); plain = answer;
  } else if (method === "generateObject") {
    // The AI SDK call starts first and the plain fetch second, in the same tick.
    const [result, answer] = await Promise.all([sdk.generateObject({ ...options, schema }), plainFetchBeside()]);
    text = result.object.answer; plain = answer;
  } else {
    const result = sdk.streamObject({ ...options, schema });
    assert(!(result instanceof Promise));
    let pending;
    for await (const _partial of result.partialObjectStream) {
      // The plain fetch is sent at the first partial object, while the application reads the stream.
      if (!pending) pending = plainFetchBeside();
    }
    assert(pending, "the stream gave no partial object");
    text = (await result.object).answer;
    plain = await pending;
  }
  assert.equal(providerCalls, 1);
  const report = traced ? auto.report() : undefined;
  if (traced) await auto.forceFlush();
  console.log(JSON.stringify({
    application: { text, plain: plain.choices[0].message.content, providerCalls },
    contextManager: contextManager(),
    active: report?.active ?? false,
    vercel: report?.integrations.find(item => item.name === "vercel")?.status ?? null,
  }));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (traced) await auto.shutdown();
});
