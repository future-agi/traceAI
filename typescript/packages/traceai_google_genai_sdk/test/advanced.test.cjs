const { test, before, after } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");
const { createRequire } = require("node:module");
const req = process.env.CONSUMER_DIR
  ? createRequire(path.join(process.env.CONSUMER_DIR, "package.json"))
  : require;
const { GoogleGenAI } = req("@google/genai");
const { instrumentGoogleGenAI } = process.env.CONSUMER_DIR
  ? req("@traceai/google-genai-sdk")
  : require("../dist");
const { NodeTracerProvider } = req("@opentelemetry/sdk-trace-node");
const { SimpleSpanProcessor, InMemorySpanExporter } = req(
  "@opentelemetry/sdk-trace-base",
);
const { context, trace, SpanStatusCode } = req("@opentelemetry/api");
const { setSession } = req("@traceai/fi-core");
let server, baseUrl, provider, handler, requests;
const memory = new InMemorySpanExporter();
const answer = (text) => ({
  candidates: [
    { content: { role: "model", parts: [{ text }] }, finishReason: "STOP" },
  ],
  usageMetadata: {
    promptTokenCount: 4,
    candidatesTokenCount: 2,
    totalTokenCount: 6,
  },
});
const interaction = (status = "completed") => ({
  id: "i-1",
  model: "gemini-test",
  status,
  outputs: [{ type: "text", text: "answer" }],
  usage: { total_input_tokens: 4, total_output_tokens: 2, total_tokens: 6 },
});
function json(res, body) {
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}
function sse(res, events) {
  res.writeHead(200, { "content-type": "text/event-stream" });
  for (const event of events) res.write(`data: ${JSON.stringify(event)}\n\n`);
  res.end();
}
const client = () =>
  new GoogleGenAI({
    apiKey: "synthetic",
    httpOptions: { baseUrl, retryOptions: { attempts: 1 } },
  });
const spans = () => memory.getFinishedSpans();
function reset() {
  memory.reset();
  requests = [];
  handler = (req, res) => json(res, answer("answer"));
}
before(async () => {
  server = http.createServer(async (req, res) => {
    const parts = [];
    for await (const p of req) parts.push(p);
    req.body = JSON.parse(Buffer.concat(parts).toString() || "{}");
    requests.push(req.body);
    handler(req, res);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  baseUrl = `http://127.0.0.1:${server.address().port}`;
  provider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(memory)],
  });
  provider.register();
  reset();
});
after(async () => {
  await provider.shutdown();
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
});

test("automatic function calls preserve request sequence and do not fabricate tool spans", async () => {
  async function run(instrumented) {
    reset();
    const ai = client();
    const handle = instrumented && instrumentGoogleGenAI(ai);
    handler = (req, res) =>
      requests.length === 1
        ? json(res, {
            candidates: [
              {
                content: {
                  role: "model",
                  parts: [
                    {
                      functionCall: {
                        name: "weather",
                        args: { city: "London" },
                      },
                    },
                  ],
                },
              },
            ],
          })
        : json(res, answer("sunny"));
    let calls = 0;
    const tool = {
      async tool() {
        return {
          functionDeclarations: [
            {
              name: "weather",
              parameters: {
                type: "OBJECT",
                properties: { city: { type: "STRING" } },
              },
            },
          ],
        };
      },
      async callTool(functionCalls) {
        calls++;
        assert.equal(functionCalls[0].name, "weather");
        return [
          {
            functionResponse: {
              name: "weather",
              response: { weather: "sunny" },
            },
          },
        ];
      },
    };
    const result = await ai.models.generateContent({
      model: "gemini-test",
      contents: "weather?",
      config: {
        tools: [tool],
        automaticFunctionCalling: { maximumRemoteCalls: 2 },
      },
    });
    if (handle) handle.dispose();
    return {
      text: result.text,
      calls,
      requests: structuredClone(requests),
      usage: result.usageMetadata,
    };
  }
  const baseline = await run(false);
  const instrumented = await run(true);
  assert.deepEqual(instrumented, baseline);
  assert.equal(instrumented.calls, 1);
  assert.equal(instrumented.requests.length, 2);
  assert.equal(spans().length, 1);
  assert.equal(
    spans()[0].attributes["gen_ai.usage.total_tokens"],
    instrumented.usage.totalTokenCount,
  );
});

test("continuation behavior is owned by the SDK, not instrumentation", async () => {
  async function run(instrumented, enabled) {
    reset();
    const ai = client();
    const handle = instrumented && instrumentGoogleGenAI(ai);
    handler = (req, res) =>
      requests.length === 1
        ? json(res, {
            candidates: [
              {
                content: { role: "model", parts: [{ text: "first" }] },
                finishReason: "CONTINUATION",
                continuationToken: "token-1",
              },
            ],
          })
        : json(res, answer("second"));
    const result = await ai.models.generateContent({
      model: "gemini-test",
      contents: "continue",
      config: { automaticContinuation: enabled },
    });
    if (handle) handle.dispose();
    return { text: result.text, requests: structuredClone(requests) };
  }
  for (const enabled of [true, false]) {
    const baseline = await run(false, enabled);
    const instrumented = await run(true, enabled);
    assert.deepEqual(instrumented, baseline);
    assert.equal(spans().length, 1);
    if (!enabled) assert.equal(requests.length, 1);
  }
});

test("caller abort preserves SDK rejection and ends once", async () => {
  reset();
  const ai = client();
  const handle = instrumentGoogleGenAI(ai);
  const abort = new AbortController();
  handler = (req, res) => {
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write(`data: ${JSON.stringify(answer("first"))}\n\n`);
  };
  const stream = await ai.models.generateContentStream({
    model: "gemini-test",
    contents: "stream",
    config: { abortSignal: abort.signal },
  });
  const iterator = stream[Symbol.asyncIterator]();
  await iterator.next();
  abort.abort();
  assert.equal(
    spans().length,
    1,
    "abort ends the span even if the caller never pulls again",
  );
  await assert.rejects(iterator.next());
  assert.equal(spans().length, 1);
  assert.equal(spans()[0].attributes["google.genai.cancelled"], true);
  handle.dispose();
});

test("per-client providers, FI sessions and concurrent streams do not leak", async () => {
  reset();
  const otherMemory = new InMemorySpanExporter();
  const otherProvider = new NodeTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(otherMemory)],
  });
  const first = client(),
    second = client();
  const a = instrumentGoogleGenAI(first),
    b = instrumentGoogleGenAI(second, {
      tracerProvider: otherProvider,
      traceConfig: { hideInputs: true, hideOutputs: true },
    });
  handler = (req, res) => sse(res, [answer("FIRST_OUTPUT_SENTINEL"), answer("SECOND_OUTPUT_SENTINEL")]);
  await Promise.all(
    [first, second].map((ai, index) =>
      context.with(
        setSession(context.active(), { sessionId: `session-${index}` }),
        async () => {
          for await (const chunk of await ai.models.generateContentStream({
            model: "gemini-test",
            contents: `input-${index}`,
          })) {
          }
        },
      ),
    ),
  );
  assert.equal(spans().length, 1);
  assert.equal(otherMemory.getFinishedSpans().length, 1);
  assert.equal(spans()[0].attributes["session.id"], "session-0");
  assert.equal(
    otherMemory.getFinishedSpans()[0].attributes["session.id"],
    "session-1",
  );
  assert.doesNotMatch(
    JSON.stringify(otherMemory.getFinishedSpans()[0].attributes),
    /input-1|FIRST_OUTPUT_SENTINEL|SECOND_OUTPUT_SENTINEL/,
  );
  a.dispose();
  b.dispose();
  await otherProvider.shutdown();
});

test("Interactions get streaming, failed responses, background submission and prior resource reference", async (t) => {
  reset();
  const ai = client();
  if (!("interactions" in ai)) return t.skip();
  const oldResource = ai.interactions;
  const handle = instrumentGoogleGenAI(ai);
  handler = (req, res) => json(res, interaction("failed"));
  await oldResource.create({
    model: "gemini-test",
    input: "failed",
    automatic_continuation: false,
  });
  assert.equal(spans()[0].status.code, SpanStatusCode.ERROR);
  handler = (req, res) => json(res, interaction("in_progress"));
  await oldResource.create({
    model: "gemini-test",
    input: "background",
    background: true,
  });
  assert.equal(requests.length, 2);
  assert.equal(spans()[1].attributes["google.genai.background"], true);
  handler = (req, res) =>
    sse(res, [
      {
        event_type: "content.delta",
        index: 0,
        delta: { type: "text", text: "hello" },
      },
      { event_type: "interaction.complete", interaction: interaction() },
    ]);
  for await (const chunk of await oldResource.get("i-1", { stream: true })) {
  }
  assert.equal(spans().length, 3);
  handle.dispose();
});

test("Interactions streams support reader and legacy readable conversion", async (t) => {
  reset();
  const ai = client();
  if (!("interactions" in ai)) return t.skip();
  const handle = instrumentGoogleGenAI(ai);
  handler = (req, res) =>
    sse(res, [
      {
        event_type: "content.delta",
        index: 0,
        delta: { type: "text", text: "hello" },
      },
      { event_type: "interaction.complete", interaction: interaction() },
    ]);
  const stream = await ai.interactions.create({
    model: "gemini-test",
    input: "stream",
    stream: true,
    automatic_continuation: false,
  });
  const readable =
    typeof stream.getReader === "function" ? stream : stream.toReadableStream();
  const reader = readable.getReader();
  let count = 0;
  for (;;) {
    const value = await reader.read();
    if (value.done) break;
    count++;
  }
  assert.equal(count, 2);
  assert.equal(spans().length, 1);
  handle.dispose();
});

test("original synchronous failures and promise rejection identity survive telemetry failures", async () => {
  reset();
  const sentinel = new Error("sdk failure");
  const ai = {
    models: {
      generateContent() {
        throw sentinel;
      },
      generateContentStream() {
        return Promise.reject(sentinel);
      },
    },
  };
  const handle = instrumentGoogleGenAI(ai, {
    tracerProvider: {
      getTracer() {
        return {
          startSpan() {
            throw new Error("telemetry failure");
          },
        };
      },
    },
  });
  assert.throws(
    () => ai.models.generateContent(),
    (error) => error === sentinel,
  );
  await assert.rejects(
    ai.models.generateContentStream(),
    (error) => error === sentinel,
  );
  handle.dispose();
});

test("optional absent resources and frozen resources remain usable", async () => {
  reset();
  const result = { candidates: [] };
  const ai = { models: Object.freeze({ generateContent: async () => result }) };
  const handle = instrumentGoogleGenAI(ai);
  assert.equal(await ai.models.generateContent(), result);
  handle.dispose();
});
