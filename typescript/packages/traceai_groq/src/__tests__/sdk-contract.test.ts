import { createServer, Server } from "node:http";
import { AddressInfo } from "node:net";
import { SpanStatusCode, context } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { GroqInstrumentation } from "../instrumentation";

// Real SDK, synthetic fetch: never send a request to a provider. CI also runs
// this file against the declared SDK floor and the pinned current release.
const proto = require(require.resolve("@opentelemetry/otlp-transformer/build/src/generated/root.js", {
  paths: [require.resolve("@opentelemetry/exporter-trace-otlp-proto")],
})).opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
const sdk = require(process.env.GROQ_TEST_SDK || "groq-sdk");
const Groq = sdk.Groq;
const chunk = (choices: unknown[], extra = {}) => ({
  id: "chat-test", object: "chat.completion.chunk", created: 1,
  model: "actual-model", choices, ...extra,
});
const chunks = [
  chunk([{ index: 1, delta: { role: "assistant", content: "second" } },
    { index: 0, delta: { role: "assistant", reasoning: "reason-", tool_calls: [
      { index: 0, id: "call-a", type: "function", function: { name: "alpha", arguments: '{"a":' } },
    ] } }]),
  chunk([{ index: 0, delta: { reasoning: "end", tool_calls: [
    { index: 1, id: "call-b", type: "function", function: { name: "beta", arguments: '{"b":' } },
  ] } }]),
  chunk([{ index: 0, delta: { tool_calls: [
    { index: 1, function: { arguments: "2}" } }, { index: 0, function: { arguments: "1}" } },
  ] }, finish_reason: "tool_calls" }, { index: 1, delta: {}, finish_reason: "stop" }]),
  chunk([], { x_groq: { usage: { prompt_tokens: 8, completion_tokens: 5, total_tokens: 13 } } }),
];
const completion = {
  ...chunk([]), object: "chat.completion",
  choices: [{ index: 0, message: { role: "assistant", content: "answer" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 8, completion_tokens: 5, total_tokens: 13 },
};
const params = { model: "requested-model", messages: [{ role: "user", content: "private-input" }] };

let reads: number;
let body: string;
let response: Response;
let fetcher: jest.Mock;
function client(streaming = true) {
  reads = 0;
  body = streaming ? chunks.map(c => `data: ${JSON.stringify(c)}\n\n`).join("") + "data: [DONE]\n\n" : JSON.stringify(completion);
  // highWaterMark zero separates SDK/user demand from Web Stream prefetch.
  const source = new ReadableStream({
    pull(controller) {
      reads++;
      controller.enqueue(new TextEncoder().encode(body));
      controller.close();
    },
  }, { highWaterMark: 0 });
  response = new Response(source, { headers: { "content-type": streaming ? "text/event-stream" : "application/json", "x-request-id": "request-test" } });
  fetcher = jest.fn(async () => response);
  return new Groq({ apiKey: "synthetic-not-a-key", maxRetries: 0, fetch: fetcher });
}
async function collect(stream: AsyncIterable<unknown>) {
  const values = [];
  for await (const value of stream) values.push(value);
  return values;
}
const tick = () => new Promise(resolve => setImmediate(resolve));

let provider: NodeTracerProvider;
let exporter: InMemorySpanExporter;
let instrumentation: GroqInstrumentation;
let server: Server;
let wire: Buffer[];
function instrument(hide = false) {
  instrumentation = new GroqInstrumentation({
    instrumentationConfig: { enabled: false },
    traceConfig: { hideInputs: hide, hideOutputs: hide },
  });
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();
  instrumentation.manuallyInstrument(sdk);
}
function unpatch() {
  // Manual patching does not register this synthetic module with require hooks.
  (instrumentation as unknown as { unpatch(module: object): void }).unpatch(sdk);
  instrumentation.disable();
}
beforeAll(async () => {
  wire = [];
  server = createServer((req, res) => {
    const data: Buffer[] = [];
    req.on("data", bytes => data.push(bytes));
    req.on("end", () => { wire.push(Buffer.concat(data)); res.end(); });
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  exporter = new InMemorySpanExporter();
  provider = new NodeTracerProvider({ spanProcessors: [
    new SimpleSpanProcessor(exporter),
    new SimpleSpanProcessor(new OTLPTraceExporter({ url: `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1/traces` })),
  ] });
  provider.register();
});
beforeEach(() => { exporter.reset(); wire.length = 0; instrument(); });
afterEach(async () => { await provider.forceFlush(); unpatch(); });
afterAll(async () => { await provider.shutdown(); await new Promise<void>(resolve => server.close(() => resolve())); });

it("preserves APIPromise helpers, identity, and lazy raw response access", async () => {
  const promise = client().chat.completions.create({ ...params, stream: true });
  expect(promise).toBeInstanceOf(Promise);
  expect(typeof promise.withResponse).toBe("function");
  expect(typeof promise.asResponse).toBe("function");
  await tick();
  expect(reads).toBe(0);
  expect(await promise.asResponse()).toBe(response);
  expect(reads).toBe(0);
  expect(response.bodyUsed).toBe(false);
  expect(await response.text()).toContain("call-a");
});

it("does not consume an awaited stream until the caller requests chunks", async () => {
  const stream = await client().chat.completions.create({ ...params, stream: true });
  await tick();
  expect(reads).toBe(0);
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  expect(typeof stream.toReadableStream).toBe("function");
  expect(stream.controller).toBeInstanceOf(AbortController);
  stream.controller.abort();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

it("aggregates explicit choice/tool indexes and terminal usage once", async () => {
  const promise = client().chat.completions.create({ ...params, stream: true });
  const data = await promise;
  expect(await collect(data)).toEqual(chunks);
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  const a = spans[0].attributes;
  const messages = JSON.parse(a[S.LLM_OUTPUT_MESSAGES] as string);
  expect(messages).toHaveLength(2);
  expect(messages[0].reasoning).toBe("reason-end");
  expect(messages[0].tool_calls).toEqual([
    { id: "call-a", type: "function", function: { name: "alpha", arguments: '{"a":1}' } },
    { id: "call-b", type: "function", function: { name: "beta", arguments: '{"b":2}' } },
  ]);
  expect(messages[1].content).toBe("second");
  expect(a[S.LLM_TOKEN_COUNT_TOTAL]).toBe(13);
  expect(a[S.LLM_TOKEN_COUNT_PROMPT]).toBe(8);
  expect(a[S.LLM_TOKEN_COUNT_COMPLETION]).toBe(5);
  expect(a[S.GEN_AI_RESPONSE_MODEL]).toBe("actual-model");
  expect(a[S.GEN_AI_RESPONSE_ID]).toBe("chat-test");
  expect(JSON.parse(a[S.GEN_AI_RESPONSE_FINISH_REASONS] as string)).toEqual(["tool_calls", "stop"]);
  expect(spans[0].status.code).toBe(SpanStatusCode.OK);
  await provider.forceFlush();
  expect(wire).toHaveLength(1);
  const decoded = proto.decode(wire[0]);
  const wireSpans = decoded.resourceSpans[0].scopeSpans[0].spans;
  expect(wireSpans).toHaveLength(1);
  const attributes = Object.fromEntries(wireSpans[0].attributes.map((a: { key: string; value: unknown }) => [a.key, a.value]));
  expect(Number(attributes[S.LLM_TOKEN_COUNT_TOTAL].intValue)).toBe(13);
  expect(Number(attributes[S.LLM_TOKEN_COUNT_PROMPT].intValue)).toBe(8);
  expect(Number(attributes[S.LLM_TOKEN_COUNT_COMPLETION].intValue)).toBe(5);
  expect(JSON.parse(attributes[S.LLM_OUTPUT_MESSAGES].stringValue)).toEqual(messages);
  expect(attributes[S.GEN_AI_RESPONSE_MODEL].stringValue).toBe("actual-model");
});

it("preserves withResponse identity and mixed helper access without premature completion", async () => {
  const promise = client().chat.completions.create({ ...params, stream: true });
  const { data, response: raw } = await promise.withResponse();
  expect(raw).toBe(response);
  expect(await promise).toBe(data);
  expect(await promise.asResponse()).toBe(raw);
  expect(reads).toBe(0);
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  expect(await collect(data)).toEqual(chunks);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBe(13);
});

it("keeps empty completions and usage-only zero token chunks", async () => {
  const c = client();
  body = `data: ${JSON.stringify(chunk([], { usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 } }))}\n\ndata: [DONE]\n\n`;
  await collect(await c.chat.completions.create({ ...params, stream: true }));
  const a = exporter.getFinishedSpans()[0].attributes;
  expect(a[S.LLM_TOKEN_COUNT_TOTAL]).toBe(0);
  expect(a[S.LLM_OUTPUT_MESSAGES]).toBe("[]");
});

it("forwards iterator throw and exposes the caller's exact error", async () => {
  const stream = await client().chat.completions.create({ ...params, stream: true });
  const iterator = stream[Symbol.asyncIterator]();
  await iterator.next();
  const error = new Error("caller-error");
  await expect(iterator.throw(error)).rejects.toBe(error);
  expect(stream.controller.signal.aborted).toBe(true);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
});

it("does not advance after a single next", async () => {
  const stream = await client().chat.completions.create({ ...params, stream: true });
  const iterator = stream[Symbol.asyncIterator]();
  expect((await iterator.next()).value).toEqual(chunks[0]);
  await tick();
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  await iterator.return();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBeUndefined();
});

it("preserves user tee without double-counting chunks or ending twice", async () => {
  const stream = await client().chat.completions.create({ ...params, stream: true });
  const [a, b] = stream.tee();
  expect(reads).toBe(0);
  expect(await collect(a)).toEqual(chunks);
  expect(await collect(b)).toEqual(chunks);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBe(13);
});

it("preserves readable stream conversion", async () => {
  const stream = await client().chat.completions.create({ ...params, stream: true });
  const reader = stream.toReadableStream().getReader();
  let text = "";
  while (true) { const item = await reader.read(); if (item.done) break; text += new TextDecoder().decode(item.value); }
  expect(text.trim().split("\n").map(line => JSON.parse(line))).toEqual(chunks);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

it("preserves readable-stream cancellation", async () => {
  const stream = await client().chat.completions.create({ ...params, stream: true });
  const reader = stream.toReadableStream().getReader();
  await reader.read();
  await reader.cancel();
  expect(stream.controller.signal.aborted).toBe(true);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBeUndefined();
});

it("keeps request options and AbortSignal cancellation working", async () => {
  const c = client(false);
  const controller = new AbortController();
  controller.abort();
  const promise = c.chat.completions.create(params, { signal: controller.signal, headers: { "x-test": "forwarded" } });
  await expect(promise).rejects.toThrow();
  expect(fetcher).not.toHaveBeenCalled();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
});

it("forwards custom request headers without changing the request", async () => {
  const c = client(false);
  await c.chat.completions.create(params, { headers: { "x-test": "forwarded" } });
  const headers = new Headers(fetcher.mock.calls[0][1].headers);
  expect(headers.get("x-test")).toBe("forwarded");
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual(params);
});

it("closes the underlying SDK stream on early break without draining it", async () => {
  const stream = await client().chat.completions.create({ ...params, stream: true });
  for await (const unused of stream) { void unused; break; }
  expect(stream.controller.signal.aborted).toBe(true);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBeUndefined();
});

it("keeps SDK errors observable and ends the span exactly once", async () => {
  const c = client();
  body = 'data: {"error":{"message":"synthetic-failure"}}\n\n';
  const stream = await c.chat.completions.create({ ...params, stream: true });
  await expect(collect(stream)).rejects.toThrow("synthetic-failure");
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
});

it("preserves non-stream results and promise catch/finally", async () => {
  const promise = client(false).chat.completions.create(params);
  const finallyCallback = jest.fn();
  expect(await promise.finally(finallyCallback)).toEqual(completion);
  expect(await promise.catch(() => null)).toEqual(completion);
  expect((await promise.withResponse()).data).toEqual(completion);
  expect(finallyCallback).toHaveBeenCalledTimes(1);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBe(13);
});

it("preserves rejected API promises and raw response errors", async () => {
  const c = client(false);
  fetcher.mockRejectedValue(new Error("synthetic transport failure"));
  const promise = c.chat.completions.create(params);
  await expect(promise.asResponse()).rejects.toThrow();
  await expect(promise).rejects.toThrow();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
});

it("honors tracing suppression", async () => {
  await context.with(suppressTracing(context.active()), () => client(false).chat.completions.create(params));
  expect(exporter.getFinishedSpans()).toHaveLength(0);
});

it("redacts input, reasoning, tools, and raw output on the OTLP wire", async () => {
  unpatch(); instrument(true);
  await collect(await client().chat.completions.create({ ...params, stream: true }));
  await provider.forceFlush();
  expect(wire).toHaveLength(1);
  const payload = wire[0].toString("utf8");
  for (const secret of ["private-input", "reason-end", "call-a", "call-b", "alpha", "beta"]) {
    expect(payload).not.toContain(secret);
  }
  expect(exporter.getFinishedSpans()[0].attributes[S.LLM_TOKEN_COUNT_TOTAL]).toBe(13);
});
