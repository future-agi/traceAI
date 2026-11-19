import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { OpenAIInstrumentation } from "../instrumentation";

// Real SDK parsing and streaming, with an in-memory transport only.
const sdk = require(process.env.OPENAI_TEST_MODULE || "openai");
type API = "chat" | "completion" | "responses";
const apis: API[] = ["chat", "completion", "responses"];
const errorBody = { error: { message: "synthetic provider failure", type: "server_error" } };
const model = "fixture-model";
const completed = {
  chat: {
    id: "chat-fixture", object: "chat.completion", created: 1, model,
    choices: [{ index: 0, message: { role: "assistant", content: "hello" }, finish_reason: "stop" }],
  },
  completion: {
    id: "completion-fixture", object: "text_completion", created: 1, model,
    choices: [{ index: 0, text: "hello", finish_reason: "stop", logprobs: null }],
  },
  responses: {
    id: "response-fixture", object: "response", status: "completed", model,
    output: [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "hello" }] }],
  },
};
const spanNames = {
  chat: "OpenAI Chat Completions",
  completion: "OpenAI Completions",
  responses: "OpenAI Responses",
  embeddings: "OpenAI Embeddings",
};

function create(client: any, api: API, stream?: boolean) {
  const options = stream === undefined ? {} : { stream };
  if (api === "chat") return client.chat.completions.create({
    model, messages: [{ role: "user", content: "hello" }], ...options,
  });
  if (api === "completion") return client.completions.create({ model, prompt: "hello", ...options });
  return client.responses.create({ model, input: "hello", ...options });
}

function clientFor(payload: unknown, status = 200, sse = false) {
  const fetch = jest.fn(async () => new Response(sse ? payload as string : JSON.stringify(payload), {
    status,
    headers: { "content-type": sse ? "text/event-stream" : "application/json" },
  }));
  return {
    client: new sdk.OpenAI({ apiKey: "synthetic-key", baseURL: "http://provider.invalid/v1", maxRetries: 0, fetch }),
    fetch,
  };
}

let exporter: InMemorySpanExporter;
let provider: BasicTracerProvider;
let instrumentation: OpenAIInstrumentation;
const savedEnv = { ...process.env };
beforeEach(() => {
  for (const key of Object.keys(process.env)) {
    if (/^(FI_|OTEL_)/.test(key)) delete process.env[key];
  }
  exporter = new InMemorySpanExporter();
  provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  instrumentation = new OpenAIInstrumentation({ instrumentationConfig: { enabled: false } });
  instrumentation.setTracerProvider(provider);
  instrumentation.manuallyInstrument(sdk);
});
afterEach(async () => {
  instrumentation.disable();
  await provider.shutdown();
  process.env = { ...savedEnv };
});

async function onlySpan(api: API | "embeddings") {
  await provider.forceFlush();
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  expect(spans[0].name).toBe(spanNames[api]);
  return spans[0];
}

function expectNoOutput(attributes: Record<string, unknown>) {
  for (const name of ["output.value", "output.mime_type", "raw.output", "llm.output_messages"]) {
    expect(attributes[name]).toBeUndefined();
  }
}

describe.each(apis)("non-stream response regressions: %s", api => {
  test.each([undefined, false])("returns an HTTP 200 error body and exports its error (stream=%s)", async stream => {
    const { client, fetch } = clientFor(errorBody);
    const promise = create(client, api, stream);
    const result = await promise;
    expect(result).toEqual(errorBody);
    expect(await promise).toBe(result);
    expect(fetch).toHaveBeenCalledTimes(1);
    const span = await onlySpan(api);
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: errorBody.error.message });
    expectNoOutput(span.attributes);
  });

  test("preserves withResponse data and exports the provider error once", async () => {
    const { client, fetch } = clientFor(errorBody);
    const promise = create(client, api);
    const { data, response } = await promise.withResponse();
    expect(data).toEqual(errorBody);
    expect(response.status).toBe(200);
    expect(await promise).toBe(data);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect((await onlySpan(api)).status).toEqual({ code: SpanStatusCode.ERROR, message: errorBody.error.message });
  });

  test.each([
    { label: "empty object", body: {} },
    { label: "unknown envelope", body: { metadata: "accepted" } },
    { label: "non-callable iterator", body: { iterator: "not-a-function" } },
    { label: "null", body: null },
    { label: "primitive", body: "unexpected response" },
    { label: "invalid choices", body: { choices: null } },
  // The Responses SDK itself rejects null/primitives before instrumentation.
  ].filter(({ body }) => api !== "responses" || (body !== null && typeof body === "object")))("returns $label without output attributes or a telemetry exception", async ({ body }) => {
    const { client, fetch } = clientFor(body);
    await expect(create(client, api)).resolves.toEqual(body);
    expect(fetch).toHaveBeenCalledTimes(1);
    const span = await onlySpan(api);
    expect(span.status.code).toBe(SpanStatusCode.UNSET);
    expectNoOutput(span.attributes);
  });
});

const encodings = [undefined, "float", "base64"] as const;
function createEmbedding(client: any, encoding?: typeof encodings[number]) {
  return client.embeddings.create({
    model, input: "hello", ...(encoding === undefined ? {} : { encoding_format: encoding }),
  });
}

describe("non-stream response regressions: embeddings", () => {
  test.each(encodings)("returns an HTTP 200 error body unchanged (encoding=%s)", async encoding => {
    const { client, fetch } = clientFor(errorBody);
    const promise = createEmbedding(client, encoding);
    const result = await promise;
    expect(result).toEqual(errorBody);
    expect(await promise).toBe(result);
    expect(fetch).toHaveBeenCalledTimes(1);
    const span = await onlySpan("embeddings");
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: errorBody.error.message });
    expectNoOutput(span.attributes);
  });

  test.each(encodings)("preserves withResponse data and ends once (encoding=%s)", async encoding => {
    const { client, fetch } = clientFor(errorBody);
    const promise = createEmbedding(client, encoding);
    const { data, response } = await promise.withResponse();
    expect(data).toEqual(errorBody);
    expect(response.status).toBe(200);
    expect(await promise).toBe(data);
    expect((await promise.withResponse()).data).toBe(data);
    expect(fetch).toHaveBeenCalledTimes(1);
    const span = await onlySpan("embeddings");
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: errorBody.error.message });
    expectNoOutput(span.attributes);
  });

  test("prioritizes the error over otherwise valid embedding data", async () => {
    const body = { ...errorBody, data: [{ index: 0, embedding: [0.5] }] };
    const { client } = clientFor(body);
    await expect(createEmbedding(client, "float")).resolves.toEqual(body);
    const span = await onlySpan("embeddings");
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: errorBody.error.message });
    expect(span.attributes["embedding.embeddings.0.embedding.vector"]).toBeUndefined();
    expectNoOutput(span.attributes);
  });

  test.each([
    { label: "missing data", body: {} },
    { label: "null data", body: { data: null } },
    { label: "object data", body: { data: {} } },
    { label: "string data", body: { data: "unexpected" } },
  ])("does not throw while observing $label", async ({ body }) => {
    // Explicit encoding bypasses the SDK's native base64 decoding validation.
    const { client } = clientFor(body);
    await expect(createEmbedding(client, "float")).resolves.toEqual(body);
    const span = await onlySpan("embeddings");
    expect(span.attributes["embedding.embeddings.0.embedding.vector"]).toBeUndefined();
  });
});

describe("base controls: embeddings", () => {
  test.each(encodings)("the uninstrumented SDK returns the error body (encoding=%s)", async encoding => {
    instrumentation.disable();
    const { client, fetch } = clientFor(errorBody);
    await expect(createEmbedding(client, encoding)).resolves.toEqual(errorBody);
    expect(fetch).toHaveBeenCalledTimes(1);
    await provider.forceFlush();
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  test.each(encodings)("asResponse leaves the error body unread (encoding=%s)", async encoding => {
    const { client, fetch } = clientFor(errorBody);
    const promise = createEmbedding(client, encoding);
    const response = await promise.asResponse();
    expect(response.status).toBe(200);
    expect(response.bodyUsed).toBe(false);
    expect(await promise.asResponse()).toBe(response);
    await expect(response.json()).resolves.toEqual(errorBody);
    expect(fetch).toHaveBeenCalledTimes(1);
    // Raw-only access records HTTP metadata; telemetry must not parse the body.
    const span = await onlySpan("embeddings");
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(span.attributes["http.response.status_code"]).toBe(200);
    expectNoOutput(span.attributes);
  });

  test("normal embeddings keep vector and usage attributes", async () => {
    const body = { object: "list", model, data: [{ object: "embedding", index: 0, embedding: [0.5] }],
      usage: { prompt_tokens: 1, total_tokens: 1 } };
    const { client } = clientFor(body);
    await expect(createEmbedding(client, "float")).resolves.toEqual(body);
    const span = await onlySpan("embeddings");
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(span.attributes["embedding.embeddings.0.embedding.vector"]).toEqual([0.5]);
    expect(span.attributes["gen_ai.usage.total_tokens"]).toBe(1);
  });
});

describe("base controls: streaming chat errors", () => {
  test.each([
    { label: "first error envelope", event: "", afterContent: false },
    { label: "first named error event", event: "event: error\n", afterContent: false },
    { label: "error envelope after content", event: "", afterContent: true },
    { label: "named error event after content", event: "event: error\n", afterContent: true },
  ])("preserves the SDK rejection for $label", async ({ event, afterContent }) => {
    const chunk = { id: "chat-fixture", object: "chat.completion.chunk", created: 1, model,
      choices: [{ index: 0, delta: { content: "hello" }, finish_reason: null }] };
    const sse = (afterContent ? `data: ${JSON.stringify(chunk)}\n\n` : "")
      + `${event}data: ${JSON.stringify(errorBody)}\n\ndata: [DONE]\n\n`;
    async function consume() {
      const { client, fetch } = clientFor(sse, 200, true);
      const stream = await create(client, "chat", true);
      const chunks = [];
      let error: unknown;
      try { for await (const value of stream) chunks.push(value); }
      catch (caught) { error = caught; }
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(chunks).toEqual(afterContent ? [chunk] : []);
      expect(error).toBeInstanceOf(sdk.APIError);
      expect(error).toMatchObject({ message: errorBody.error.message, error: errorBody.error });
      return { error, stream };
    }
    instrumentation.disable();
    const native = await consume();
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    instrumentation.manuallyInstrument(sdk);
    const traced = await consume();
    expect((traced.error as Error).constructor).toBe((native.error as Error).constructor);
    const span = await onlySpan("chat");
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: errorBody.error.message });
    expect(span.events.filter(event => event.name === "exception")).toHaveLength(1);
    expectNoOutput(span.attributes);
    // SDK cleanup aborts its controller before rejecting; it must not end twice.
    traced.stream.controller.abort();
    await provider.forceFlush();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });
});

describe.each(apis)("base controls: %s", api => {
  test("the uninstrumented SDK returns the same HTTP 200 error body", async () => {
    instrumentation.disable();
    const { client, fetch } = clientFor(errorBody);
    await expect(create(client, api)).resolves.toEqual(errorBody);
    expect(fetch).toHaveBeenCalledTimes(1);
    await provider.forceFlush();
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  test("a normal completion still exports one successful span", async () => {
    const { client, fetch } = clientFor(completed[api]);
    // The SDK may add conveniences such as output_text to the parsed body.
    await expect(create(client, api)).resolves.toMatchObject(completed[api]);
    expect(fetch).toHaveBeenCalledTimes(1);
    const span = await onlySpan(api);
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(span.attributes["output.value"]).toBeDefined();
  });

  test("a real SDK stream stays lazy and exports one successful span on consumption", async () => {
    const chunk = api === "chat" ? {
      id: "chat-fixture", object: "chat.completion.chunk", created: 1, model,
      choices: [{ index: 0, delta: { role: "assistant", content: "hello" }, finish_reason: "stop" }],
    } : api === "completion" ? completed.completion : { type: "response.completed", response: completed.responses };
    const { client, fetch } = clientFor(`data: ${JSON.stringify(chunk)}\n\ndata: [DONE]\n\n`, 200, true);
    const stream = await create(client, api, true);
    expect(typeof stream[Symbol.asyncIterator]).toBe("function");
    expect(stream.controller).toBeInstanceOf(AbortController);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    const chunks = [];
    for await (const value of stream) chunks.push(value);
    expect(chunks).toEqual([chunk]);
    expect(fetch).toHaveBeenCalledTimes(1);
    const span = await onlySpan(api);
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(span.attributes["output.value"]).toBeDefined();
  });

  test("an HTTP 500 still rejects with the SDK error and exports one failed span", async () => {
    const { client, fetch } = clientFor(errorBody, 500);
    const promise = create(client, api);
    await expect(promise).rejects.toBeInstanceOf(sdk.APIError);
    await expect(promise).rejects.toMatchObject({ status: 500, error: errorBody.error });
    expect(fetch).toHaveBeenCalledTimes(1);
    const span = await onlySpan(api);
    expect(span.status.code).toBe(SpanStatusCode.ERROR);
    expect(span.status.message).toContain(errorBody.error.message);
  });
});
