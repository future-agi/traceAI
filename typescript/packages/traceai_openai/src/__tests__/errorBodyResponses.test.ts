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

async function onlySpan(api: API) {
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
