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
const { diag, DiagLogLevel } = req("@opentelemetry/api");
let server, baseUrl, provider, handler, requests;
const memory = new InMemorySpanExporter();
const answer = (text) => ({
  candidates: [
    { content: { role: "model", parts: [{ text }] }, finishReason: "STOP" },
  ],
});
const interaction = () => ({
  id: "i-1",
  model: "gemini-test",
  status: "completed",
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

test("late usage, multiple candidates, reasoning and repeated tool parts survive SSE", async () => {
  reset();
  const ai = client();
  const handle = instrumentGoogleGenAI(ai);
  handler = (req, res) =>
    sse(res, [
      {
        candidates: [
          {
            index: 0,
            content: {
              role: "model",
              parts: [
                { text: "reason", thought: true },
                { functionCall: { name: "weather", args: { city: "London" } } },
              ],
            },
          },
          {
            index: 1,
            content: { role: "model", parts: [{ text: "alternative" }] },
          },
        ],
      },
      {
        candidates: [
          {
            index: 0,
            content: {
              role: "model",
              parts: [
                { functionCall: { name: "weather", args: { city: "London" } } },
              ],
            },
            finishReason: "STOP",
          },
        ],
        usageMetadata: {
          promptTokenCount: 9,
          candidatesTokenCount: 7,
          thoughtsTokenCount: 2,
          cachedContentTokenCount: 3,
          totalTokenCount: 18,
        },
      },
    ]);
  const result = [];
  for await (const chunk of await ai.models.generateContentStream({
    model: "gemini-test",
    contents: "question",
  }))
    result.push(chunk);
  assert.equal(result.length, 2);
  assert.equal(result[0].candidates.length, 2);
  assert.equal(result[0].candidates[0].content.parts[0].thought, true);
  const attrs = spans()[0].attributes;
  assert.equal(attrs["gen_ai.usage.total_tokens"], 18);
  assert.equal(attrs["gen_ai.usage.output_tokens.reasoning"], 2);
  assert.equal(attrs["gen_ai.usage.cache_read_tokens"], 3);
  assert.equal(
    (attrs["gen_ai.output.messages"].match(/weather/g) || []).length,
    2,
  );
  assert.match(attrs["gen_ai.output.messages"], /alternative/);
  handle.dispose();
});

test("blocked/no-candidate and missing/empty embedding responses preserve values without invented usage", async () => {
  for (const body of [
    { promptFeedback: { blockReason: "SAFETY" } },
    {},
    { embeddings: [] },
  ]) {
    reset();
    const ai = client();
    const handle = instrumentGoogleGenAI(ai);
    handler = (req, res) => json(res, body);
    const result = await ai.models.generateContent({
      model: "gemini-test",
      contents: "blocked",
    });
    assert.equal(result.candidates, undefined);
    const embeddings = await ai.models.embedContent({
      model: "text-embedding-004",
      contents: "empty",
    });
    assert.ok(embeddings);
    assert.equal(spans().length, 2);
    assert.ok(
      spans().every(
        (s) => s.attributes["gen_ai.usage.total_tokens"] === undefined,
      ),
    );
    handle.dispose();
  }
});

test("chat automatic tools enabled and disabled have the same requests with and without tracing", async () => {
  async function run(instrumented, disabled) {
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
                  parts: [{ functionCall: { name: "weather", args: {} } }],
                },
              },
            ],
          })
        : json(res, answer("sunny"));
    let calls = 0;
    const tool = {
      async tool() {
        return { functionDeclarations: [{ name: "weather" }] };
      },
      async callTool() {
        calls++;
        return [
          {
            functionResponse: {
              name: "weather",
              response: { result: "sunny" },
            },
          },
        ];
      },
    };
    const chat = ai.chats.create({
      model: "gemini-test",
      config: {
        tools: [tool],
        automaticFunctionCalling: { disable: disabled, maximumRemoteCalls: 2 },
      },
    });
    const result = await chat.sendMessage({ message: "weather" });
    if (handle) handle.dispose();
    return {
      calls,
      requests: structuredClone(requests),
      candidates: result.candidates,
      history: chat.getHistory(),
    };
  }
  for (const disabled of [true, false]) {
    const baseline = await run(false, disabled);
    const observed = await run(true, disabled);
    assert.deepEqual(observed, baseline);
    assert.equal(observed.calls, disabled ? 0 : 1);
    assert.equal(spans().length, 1);
  }
});

test("Interactions tool turns, continuation and disabled continuation preserve upstream behavior", async (t) => {
  if (!("interactions" in client())) return t.skip();
  async function run(instrumented, enabled) {
    reset();
    const ai = client();
    const handle = instrumented && instrumentGoogleGenAI(ai);
    handler = (req, res) =>
      requests.length === 1
        ? json(res, {
            ...interaction(),
            status: "incomplete",
            continuation_token: "continue-1",
          })
        : json(res, interaction());
    const result = await ai.interactions.create({
      model: "gemini-test",
      previous_interaction_id: "prior",
      input: [
        {
          type: "function_result",
          name: "weather",
          call_id: "call-1",
          result: "sunny",
        },
      ],
      automatic_continuation: enabled,
    });
    if (handle) handle.dispose();
    return {
      status: result.status,
      outputs: result.outputs,
      requests: structuredClone(requests),
    };
  }
  for (const enabled of [true, false]) {
    const baseline = await run(false, enabled);
    const observed = await run(true, enabled);
    assert.deepEqual(observed, baseline);
    assert.equal(spans().length, 1);
    assert.match(
      spans()[0].attributes["gen_ai.input.messages"],
      /function_result/,
    );
    if (!enabled) assert.equal(requests.length, 1);
  }
});

test("stream branching stays functional and explicitly ends request-only telemetry", async (t) => {
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
  const [left, right] = stream.tee();
  const read = async (stream) => {
    const all = [];
    for await (const chunk of stream) all.push(chunk);
    return all;
  };
  assert.deepEqual(await read(left), await read(right));
  assert.equal(spans().length, 1);
  assert.equal(
    spans()[0].attributes["google.genai.stream_capture"],
    "request_only",
  );
  handle.dispose();
});

test("telemetry failures log only a static diagnostic without content or secrets", () => {
  reset();
  const logs = [];
  diag.setLogger(
    {
      warn: (...args) => logs.push(args),
      error() {},
      info() {},
      debug() {},
      verbose() {},
    },
    DiagLogLevel.WARN,
  );
  try {
    const ai = {
      models: {
        generateContent() {
          return "original result";
        },
      },
    };
    const handle = instrumentGoogleGenAI(ai, {
      tracerProvider: {
        getTracer() {
          return {
            startSpan() {
              throw new Error("PRIVATE_SENTINEL synthetic-key");
            },
          };
        },
      },
    });
    assert.equal(ai.models.generateContent(), "original result");
    assert.ok(logs.length);
    assert.doesNotMatch(JSON.stringify(logs), /PRIVATE_SENTINEL|synthetic-key/);
    handle.dispose();
  } finally {
    diag.disable();
  }
});

test("Interactions preserves version-specific promise helpers and unread raw responses", async () => {
  reset();
  const ai = client();
  const handle = instrumentGoogleGenAI(ai);
  handler = (req, res) => json(res, interaction());
  const pending = ai.interactions.create({
    model: "gemini-test",
    input: "helpers",
    automatic_continuation: false,
  });
  if (typeof pending.withResponse !== "function") {
    // Newer GoogleGenAI versions deliberately return a plain Promise instead.
    assert.equal((await pending).id, "i-1");
    assert.equal(spans().length, 1);
    handle.dispose();
    return;
  }
  const envelope = await pending.withResponse();
  assert.equal(envelope.data.id, "i-1");
  assert.equal(envelope.response.status, 200);
  assert.equal(await pending, envelope.data);
  assert.equal(spans().length, 1);
  const raw = await ai.interactions
    .create({
      model: "gemini-test",
      input: "raw",
      automatic_continuation: false,
    })
    .asResponse();
  assert.equal(
    raw.bodyUsed,
    false,
    "instrumentation must not eagerly consume a raw response",
  );
  assert.equal((await raw.json()).id, "i-1");
  assert.equal(spans().length, 2);
  assert.equal(
    spans()[1].attributes["google.genai.response_capture"],
    "request_only",
  );
  handler = (req, res) =>
    sse(res, [
      {
        event_type: "content.delta",
        index: 0,
        delta: { type: "text", text: "hello" },
      },
      { event_type: "interaction.complete", interaction: interaction() },
    ]);
  const streaming = ai.interactions.create({
    model: "gemini-test",
    input: "stream",
    stream: true,
    automatic_continuation: false,
  });
  const { data } = await streaming.withResponse();
  assert.equal(data, await streaming);
  assert.equal(spans().length, 2);
  let count = 0;
  for await (const event of data) count++;
  assert.equal(count, 2);
  assert.equal(spans().length, 3);
  assert.equal(requests.length, 3);
  handle.dispose();
});
