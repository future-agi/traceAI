const { test, before, after } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");
const { createRequire } = require("node:module");
const requireConsumer = process.env.CONSUMER_DIR
  ? createRequire(path.join(process.env.CONSUMER_DIR, "package.json"))
  : require;
const { GoogleGenAI } = requireConsumer("@google/genai");
const { instrumentGoogleGenAI, VERSION } = process.env.CONSUMER_DIR
  ? requireConsumer("@traceai/google-genai-sdk")
  : require("../dist");
const { context, trace, SpanStatusCode } =
  requireConsumer("@opentelemetry/api");
const { suppressTracing } = requireConsumer("@opentelemetry/core");
const { NodeTracerProvider } = requireConsumer("@opentelemetry/sdk-trace-node");
const { SimpleSpanProcessor, InMemorySpanExporter } = requireConsumer(
  "@opentelemetry/sdk-trace-base",
);
const { OTLPTraceExporter } = requireConsumer(
  "@opentelemetry/exporter-trace-otlp-http",
);
const { setSession } = requireConsumer("@traceai/fi-core");
const memory = new InMemorySpanExporter();
let server,
  baseUrl,
  provider,
  reply,
  requests = [],
  wire = [];
const completion = (text = "answer") => ({
  candidates: [
    {
      index: 0,
      content: { role: "model", parts: [{ text }] },
      finishReason: "STOP",
    },
  ],
  modelVersion: "gemini-test",
  responseId: "response-1",
  usageMetadata: {
    promptTokenCount: 5,
    candidatesTokenCount: 3,
    totalTokenCount: 8,
  },
});
const interaction = () => ({
  id: "interaction-1",
  model: "gemini-test",
  status: "completed",
  outputs: [{ type: "text", text: "interaction answer" }],
  usage: { total_input_tokens: 5, total_output_tokens: 3, total_tokens: 8 },
});
function json(res, value, status = 200) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(value));
}
function sse(res, values) {
  res.writeHead(200, { "content-type": "text/event-stream" });
  for (const value of values) res.write(`data: ${JSON.stringify(value)}\n\n`);
  res.end();
}
function defaults(req, res) {
  if (req.url.includes("streamGenerateContent"))
    return sse(res, [
      completion("same"),
      completion("same"),
      completion("different"),
    ]);
  if (
    req.url.includes("embedContent") ||
    req.url.includes("batchEmbedContents")
  )
    return json(res, {
      embeddings: [{ values: [0.1, 0.2] }, { values: [0.3, 0.4] }],
      embedding: { values: [0.1, 0.2] },
    });
  if (req.url.includes("predictLongRunning"))
    return json(res, { name: "operations/video-1", done: false });
  if (req.url.includes(":predict"))
    return json(res, {
      predictions: [
        { bytesBase64Encoded: "BINARY_SENTINEL", mimeType: "image/png" },
      ],
    });
  if (req.url.includes("interactions")) {
    if (req.method === "DELETE") return json(res, {});
    if (req.body.stream || req.url.includes("stream=true"))
      return sse(res, [
        {
          event_type: "interaction.start",
          interaction: { id: "interaction-1", status: "in_progress" },
        },
        {
          event_type: "content.delta",
          index: 0,
          delta: { type: "text", text: "interaction answer" },
        },
        { event_type: "interaction.complete", interaction: interaction() },
      ]);
    return json(res, interaction());
  }
  return json(res, completion());
}
function client(extra = {}) {
  return new GoogleGenAI({
    apiKey: "synthetic-google-key",
    httpOptions: { baseUrl, retryOptions: { attempts: 1 } },
    ...extra,
  });
}
function reset() {
  memory.reset();
  requests = [];
  wire = [];
  reply = defaults;
}
function spans() {
  return memory
    .getFinishedSpans()
    .filter((s) => s.instrumentationScope.name === "@traceai/google-genai-sdk");
}
before(async () => {
  server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks).toString();
    if (req.url === "/v1/traces") {
      wire.push({ body: JSON.parse(body), headers: req.headers });
      return json(res, {});
    }
    req.body = body ? JSON.parse(body) : {};
    requests.push({
      url: req.url,
      method: req.method,
      body: req.body,
      headers: req.headers,
    });
    try {
      reply(req, res);
    } catch (error) {
      json(res, { error: { code: 500, message: error.message } }, 500);
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  baseUrl = `http://127.0.0.1:${server.address().port}`;
  provider = new NodeTracerProvider({
    spanProcessors: [
      new SimpleSpanProcessor(memory),
      new SimpleSpanProcessor(
        new OTLPTraceExporter({
          url: `${baseUrl}/v1/traces`,
          headers: {
            "x-api-key": "synthetic-fi-key",
            "x-secret-key": "synthetic-fi-secret",
          },
        }),
      ),
    ],
  });
  provider.register();
  reset();
});
after(async () => {
  await provider.shutdown();
  await new Promise((resolve) => server.close(resolve));
});

test("native baseline, generation, parent, tools, config and safe media", async () => {
  reset();
  const ai = client();
  await ai.models.generateContent({
    model: "gemini-test",
    contents: "baseline",
  });
  assert.equal(spans().length, 0);
  const handle = instrumentGoogleGenAI(ai);
  const parent = trace.getTracer("app").startSpan("parent");
  const result = await context.with(
    trace.setSpan(context.active(), parent),
    () =>
      ai.models.generateContent({
        model: "gemini-test",
        contents: [
          {
            role: "user",
            parts: [
              { text: "question" },
              {
                inlineData: { data: "BINARY_SENTINEL", mimeType: "image/png" },
              },
            ],
          },
        ],
        config: {
          temperature: 0.2,
          systemInstruction: "system instruction",
          tools: [
            {
              functionDeclarations: [
                {
                  name: "weather",
                  description: "tool description",
                  parameters: {
                    type: "OBJECT",
                    properties: { city: { type: "STRING" } },
                  },
                },
              ],
            },
          ],
        },
      }),
  );
  parent.end();
  assert.equal(result.text, "answer");
  assert.equal(spans().length, 1);
  const span = spans()[0];
  assert.equal(span.parentSpanContext.spanId, parent.spanContext().spanId);
  assert.equal(span.attributes["gen_ai.request.model"], "gemini-test");
  assert.equal(span.attributes["gen_ai.usage.total_tokens"], 8);
  assert.match(span.attributes["gen_ai.input.messages"], /question/);
  assert.match(span.attributes["gen_ai.tool.definitions"], /weather/);
  assert.doesNotMatch(
    JSON.stringify(span.attributes),
    /BINARY_SENTINEL|synthetic-google-key/,
  );
  assert.match(span.attributes["gen_ai.request.parameters"], /0.2/);
  handle.dispose();
});

test("SSE preserves repeated and distinct parts, late usage and parent", async () => {
  reset();
  const ai = client();
  const handle = instrumentGoogleGenAI(ai);
  const stream = await ai.models.generateContentStream({
    model: "gemini-test",
    contents: "stream",
  });
  assert.equal(spans().length, 0);
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk.text);
  assert.deepEqual(chunks, ["same", "same", "different"]);
  assert.equal(spans().length, 1);
  assert.equal(spans()[0].attributes["gen_ai.usage.total_tokens"], 8);
  assert.equal(
    (spans()[0].attributes["gen_ai.output.messages"].match(/same/g) || [])
      .length,
    2,
  );
  handle.dispose();
});

test("empty stream, break, return before first read, throw, prestream and midstream errors", async () => {
  for (const mode of [
    "empty",
    "break",
    "return",
    "throw",
    "pre-error",
    "mid-error",
  ]) {
    reset();
    const ai = client();
    const handle = instrumentGoogleGenAI(ai);
    reply = (req, res) =>
      mode === "empty"
        ? sse(res, [])
        : mode === "pre-error"
          ? json(res, { error: { code: 400, message: "ERROR_SENTINEL" } }, 400)
          : mode === "mid-error"
            ? (res.writeHead(200, { "content-type": "text/event-stream" }),
              res.end(
                `data: ${JSON.stringify(completion())}\n\ndata: {INVALID_JSON\n\n`,
              ))
            : defaults(req, res);
    try {
      const stream = await ai.models.generateContentStream({
        model: "gemini-test",
        contents: "test",
      });
      if (mode === "return") await stream.return();
      else if (mode === "throw")
        await assert.rejects(stream.throw(new Error("consumer error")));
      else {
        for await (const chunk of stream) {
          if (mode === "break") break;
        }
      }
    } catch (error) {
      assert.ok(mode.endsWith("error"), `${mode}: ${error}`);
    }
    assert.equal(spans().length, 1, mode);
    assert.doesNotMatch(
      JSON.stringify(spans()[0].attributes),
      /ERROR_SENTINEL/,
    );
    if (mode.endsWith("error") || mode === "throw")
      assert.equal(spans()[0].status.code, SpanStatusCode.ERROR, mode);
    handle.dispose();
  }
});

test("existing and new chats trace through models without rewriting history", async () => {
  reset();
  const ai = client();
  const before = ai.chats.create({ model: "gemini-test" });
  const handle = instrumentGoogleGenAI(ai);
  const chat = ai.chats.create({ model: "gemini-test" });
  await before.sendMessage({ message: "before setup" });
  await chat.sendMessage({ message: "first" });
  for await (const chunk of await chat.sendMessageStream({
    message: "second",
  })) {
  }
  assert.equal(spans().length, 3);
  const baseline = client().chats.create({ model: "gemini-test" });
  await baseline.sendMessage({ message: "first" });
  for await (const chunk of await baseline.sendMessageStream({
    message: "second",
  })) {
  }
  assert.deepEqual(chat.getHistory(), baseline.getHistory());
  assert.ok(
    requests
      .at(-1)
      .body.contents.some((x) => JSON.stringify(x).includes("first")),
  );
  handle.dispose();
});

test("embedding input/output count without fabricated usage or vectors", async () => {
  reset();
  const ai = client();
  const handle = instrumentGoogleGenAI(ai);
  const result = await ai.models.embedContent({
    model: "text-embedding-004",
    contents: ["one", "two"],
  });
  assert.equal(result.embeddings.length, 2);
  const attrs = spans()[0].attributes;
  assert.equal(attrs["fi.span.kind"], "EMBEDDING");
  assert.equal(attrs["embedding.count"], 2);
  assert.equal(attrs["embedding.dimensions"], 2);
  assert.equal(attrs["gen_ai.usage.total_tokens"], undefined);
  assert.doesNotMatch(JSON.stringify(attrs), /0\.1|0\.3/);
  handle.dispose();
});

test("conditional Interactions create, get, cancel, delete and SSE preserve lazy identity", async (t) => {
  reset();
  const ai = client();
  if (!("interactions" in ai)) return t.skip("SDK predates Interactions");
  const handle = instrumentGoogleGenAI(ai);
  const resource = ai.interactions;
  assert.equal(ai.interactions, resource);
  const result = await resource.create({
    model: "gemini-test",
    input: "hello",
    previous_interaction_id: "previous-1",
    automatic_continuation: false,
  });
  assert.equal(result.id, "interaction-1");
  await resource.get("interaction-1");
  if (typeof resource.cancel === "function")
    await resource.cancel("interaction-1");
  await resource.delete("interaction-1");
  const events = [];
  for await (const event of await resource.create({
    model: "gemini-test",
    input: "stream",
    stream: true,
    automatic_continuation: false,
  }))
    events.push(event);
  assert.equal(events.length, 3);
  assert.equal(spans().length, typeof resource.cancel === "function" ? 5 : 4);
  assert.equal(
    spans()[0].attributes["google.genai.previous_interaction_id"],
    "previous-1",
  );
  assert.equal(spans()[0].attributes["gen_ai.usage.total_tokens"], 8);
  assert.equal(requests.length, spans().length);
  handle.dispose();
  assert.equal(ai.interactions, resource);
});

test("image and video submissions omit binary, admin and Live stay untouched", async () => {
  reset();
  const ai = client({ vertexai: true });
  const admin = ai.models.list;
  const live = ai.live.connect;
  const handle = instrumentGoogleGenAI(ai);
  await ai.models.generateImages({
    model: "imagen-test",
    prompt: "a landscape",
  });
  await ai.models.generateVideos({ model: "veo-test", prompt: "clouds" });
  assert.equal(spans().length, 2);
  assert.doesNotMatch(
    JSON.stringify(spans().map((s) => s.attributes)),
    /BINARY_SENTINEL/,
  );
  assert.equal(spans()[1].attributes["gen_ai.operation.name"], "submit_video");
  assert.equal(spans()[1].attributes["google.genai.operation_done"], false);
  assert.equal(ai.models.list, admin);
  assert.equal(ai.live.connect, live);
  handle.dispose();
});

test("duplicate setup, suppression, disable, re-enable, dispose and independent clients", async () => {
  reset();
  const ai = client();
  const original = ai.models.generateContent;
  const handle = instrumentGoogleGenAI(ai);
  assert.equal(instrumentGoogleGenAI(ai), handle);
  handle.disable();
  await ai.models.generateContent({
    model: "gemini-test",
    contents: "disabled",
  });
  handle.enable();
  await context.with(suppressTracing(context.active()), () =>
    ai.models.generateContent({ model: "gemini-test", contents: "suppressed" }),
  );
  assert.equal(spans().length, 0);
  const other = client();
  const hidden = instrumentGoogleGenAI(other, {
    traceConfig: { hideInputs: true, hideOutputs: true },
  });
  await Promise.all([
    ai.models.generateContent({ model: "gemini-test", contents: "visible" }),
    other.models.generateContent({
      model: "gemini-test",
      contents: "HIDDEN_SENTINEL",
    }),
  ]);
  assert.equal(spans().length, 2);
  assert.doesNotMatch(
    JSON.stringify(spans().map((s) => s.attributes)),
    /HIDDEN_SENTINEL/,
  );
  handle.dispose();
  assert.equal(ai.models.generateContent, original);
  hidden.dispose();
  const again = instrumentGoogleGenAI(ai);
  await ai.models.generateContent({ model: "gemini-test", contents: "again" });
  assert.equal(spans().length, 3);
  again.dispose();
});

test("all FI privacy controls protect actual OTLP payload and credential boundaries", async () => {
  for (const env of [
    "FI_HIDE_INPUTS",
    "FI_HIDE_INPUT_MESSAGES",
    "FI_HIDE_INPUT_TEXT",
    "FI_HIDE_OUTPUTS",
    "FI_HIDE_OUTPUT_MESSAGES",
    "FI_HIDE_OUTPUT_TEXT",
    "FI_HIDE_INPUT_IMAGES",
    "FI_HIDE_EMBEDDING_VECTORS",
    "FI_HIDE_ERROR_DETAILS",
    "FI_PII_REDACTION",
    "FI_BASE64_IMAGE_MAX_LENGTH",
  ]) {
    reset();
    process.env[env] = env === "FI_BASE64_IMAGE_MAX_LENGTH" ? "1" : "true";
    const ai = client();
    const handle = instrumentGoogleGenAI(ai);
    delete process.env[env];
    reply = (req, res) =>
      json(res, completion("OUTPUT_SENTINEL jane@example.com"));
    await ai.models.generateContent({
      model: "gemini-test",
      contents: [
        {
          role: "user",
          parts: [
            { text: "INPUT_SENTINEL jane@example.com" },
            { inlineData: { data: "BINARY_SENTINEL", mimeType: "image/png" } },
          ],
        },
      ],
      config: {
        tools: [
          {
            functionDeclarations: [
              {
                name: "tool",
                description: "INPUT_SENTINEL",
                parameters: { type: "OBJECT" },
              },
            ],
          },
        ],
      },
    });
    reply = (req, res) =>
      json(
        res,
        {
          error: { code: 400, message: "ERROR_SENTINEL synthetic-google-key" },
        },
        400,
      );
    await assert.rejects(
      ai.models.generateContent({
        model: "gemini-test",
        contents: "INPUT_SENTINEL",
      }),
    );
    await provider.forceFlush();
    const bytes = JSON.stringify(wire.map((w) => w.body));
    if (
      [
        "FI_HIDE_INPUTS",
        "FI_HIDE_INPUT_MESSAGES",
        "FI_HIDE_INPUT_TEXT",
      ].includes(env)
    )
      assert.doesNotMatch(bytes, /INPUT_SENTINEL/, env);
    if (
      [
        "FI_HIDE_OUTPUTS",
        "FI_HIDE_OUTPUT_MESSAGES",
        "FI_HIDE_OUTPUT_TEXT",
      ].includes(env)
    )
      assert.doesNotMatch(bytes, /OUTPUT_SENTINEL/, env);
    if (env === "FI_PII_REDACTION")
      assert.doesNotMatch(bytes, /jane@example\.com/);
    assert.doesNotMatch(
      bytes,
      /ERROR_SENTINEL|BINARY_SENTINEL|synthetic-google-key|synthetic-fi-key|synthetic-fi-secret/,
    );
    assert.ok(wire.length >= 2);
    assert.equal(wire[0].headers["x-secret-key"], "synthetic-fi-secret");
    assert.ok(
      requests.every(
        (r) => !r.headers["x-secret-key"] && !r.headers["x-api-key"],
      ),
    );
    const exported = wire.flatMap((w) =>
      w.body.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans)),
    );
    assert.ok(
      exported.every((s) => s.traceId.length === 32 && s.spanId.length === 16),
    );
    handle.dispose();
  }
});

test("capture is bounded without dropping user chunks", async () => {
  reset();
  const ai = client();
  const handle = instrumentGoogleGenAI(ai, { maxContentLength: 256 });
  reply = (req, res) =>
    sse(
      res,
      Array.from({ length: 100 }, () => completion("x".repeat(100))),
    );
  let count = 0;
  for await (const chunk of await ai.models.generateContentStream({
    model: "gemini-test",
    contents: "stream",
  }))
    count++;
  assert.equal(count, 100);
  assert.equal(spans()[0].attributes["google.genai.capture_truncated"], true);
  assert.ok(JSON.stringify(spans()[0].attributes).length < 3000);
  handle.dispose();
});

test("Vertex API-key generation and editImage use only loopback", async () => {
  reset();
  const ai = client({ vertexai: true });
  const handle = instrumentGoogleGenAI(ai);
  await ai.models.generateContent({ model: "gemini-test", contents: "vertex" });
  await ai.models.editImage({
    model: "imagen-test",
    prompt: "blue sky",
    referenceImages: [],
  });
  assert.equal(spans().length, 2);
  assert.ok(requests.every((r) => r.headers.host.startsWith("127.0.0.1")));
  handle.dispose();
});
