import { context, createTraceState, trace } from "@opentelemetry/api";
import { BasicTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { privacyFilter } from "../src/export-privacy";
import { createFetchHook } from "../src/wire/fetch-runtime";
import { trackCoverage, shouldSkip } from "../src/wire/fetch";
import { build, captureSecrets, match } from "../src/wire/core";
import { resolveConfig } from "../src/config";
import { scrubText } from "../src/wire/privacy-runtime";

const url = "https://api.openai.com/v1/chat/completions", init = { method: "POST", body: '{"messages":[]}' };
const answer = '{"choices":[{"message":{"content":"answer"}}]}';
function fake(attributes: any = {}): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext() { return this._spanContext; }, name: "app", kind: 2, startTime: [1, 2], endTime: [3, 4], _duration: [2, 2], attributes, status: { code: 1 }, events: [], links: [], resource: { attributes: {} }, instrumentationScope: { name: "app" } };
}
function contents(span: any) { return JSON.stringify({ ...span, context: span.spanContext(), resource: span.resource.attributes, tracestate: span.spanContext().traceState?.serialize(), links: span.links.map((l: any) => ({ ...l, tracestate: l.context.traceState?.serialize() })) }); }

test("S1 snapshot covers scope, resource, link and parent tracestate without retaining raw spans", () => {
  const secret = "private-provider-token", dirty = `${secret} https://cdn/?token=signed-private data:audio/wav;base64,U0VDUkVU`;
  const source = fake({ custom: dirty, array: [dirty, NaN, Infinity, -Infinity], numeric: 12345678, numbers: [1, 12345678], "http.request.header.token": [123, true] });
  source._spanContext.traceState = createTraceState(`vendor=${secret}`);
  source.parentSpanContext = { ...source._spanContext };
  source.resource = { attributes: { custom: dirty } };
  source.instrumentationScope = { name: dirty, version: dirty, schemaUrl: dirty, attributes: { custom: dirty } };
  source.events = [{ name: dirty, time: [1, 2], attributes: { "exception.message": dirty } }];
  source.links = [{ context: source._spanContext, attributes: { custom: dirty } }];
  source.status = { code: 2, message: dirty };
  const output = privacyFilter({}, [secret, "12345678"]).span(source);
  for (const forbidden of [secret, "12345678", "signed-private", "U0VDUkVU"]) expect(contents(output)).not.toContain(forbidden);
  expect(output.attributes.array).toEqual([expect.any(String), "NaN", "Infinity", "-Infinity"]);
  expect(output.attributes["http.request.header.token"]).toBe("<redacted>");
  expect(output.attributes.numeric).toBe("<redacted>"); expect(output.attributes.numbers).toEqual([1, "<redacted>"]);
  expect(output.spanContext().traceId).toBe(source._spanContext.traceId);
  expect(output.spanContext().traceState?.serialize()).not.toContain(secret);
  expect(output.parentSpanContext?.traceState?.serialize()).not.toContain(secret);
  expect(Object.getPrototypeOf(output)).toBe(Object.prototype);
  expect(contents(source)).toContain(secret); // use an explicit context method for the source below
});

test("S1 unknown objects, accessors and proxies never execute user code", () => {
  const execute = jest.fn(() => { throw new Error("user code must not execute"); });
  class User { toString = execute; toJSON = execute; }
  const unknown = Object.defineProperty(new User(), "mimeType", { get: execute });
  const proxy = new Proxy({}, { get: execute, ownKeys: execute, getOwnPropertyDescriptor: execute });
  const attrs = Object.defineProperty({ unknown, proxy, array: [unknown, undefined, 12n] }, "getter", { get: execute, enumerable: true });
  const output = privacyFilter().span(fake(attrs));
  expect(output.attributes).toMatchObject({ unknown: "object", proxy: "object", array: ["object", "undefined", "bigint"] });
  expect(output.attributes.getter).toBeUndefined(); expect(execute).not.toHaveBeenCalled();
});
test.each(["cycle", "branch", "text"])("S1 sanitizer %s budget fails closed per span while preserving identity/timing", mode => {
  const cycle: any[] = []; cycle.push(cycle);
  const payload = mode === "cycle" ? cycle : mode === "branch" ? Array(25000).fill("private-content") : "private-content".repeat(700000);
  const span = fake({ private: "private-content", payload }); span.events = [{ name: "private-content", attributes: { value: "private-content" } }];
  const output = privacyFilter().span(span);
  expect(output.attributes).toEqual({}); expect(output.events).toEqual([]); expect(contents(output)).not.toContain("private-content");
  expect(output.startTime).toEqual([1, 2]); expect(output.endTime).toEqual([3, 4]); expect(output.spanContext().spanId).toBe("2".repeat(16));
});
test.each(["hideInputs", "hideInputMessages", "hideInputText", "hideOutputs", "hideOutputMessages", "hideOutputText"] as const)("S1 %s applies by family on every surface", flag => {
  const side = flag.includes("Input") ? "input" : "output";
  const attrs = { [`${side}.arbitrary`]: "private-content", [`gen_ai.${side}.custom`]: "private-content", "gen_ai.system_instructions": side === "input" ? "private-content" : "safe", "gen_ai.tool.definitions": "private-content" };
  const span = fake(attrs); span.resource.attributes = attrs; span.instrumentationScope.attributes = attrs; span.events = [{ name: "event", attributes: attrs }]; span.links = [{ context: span._spanContext, attributes: attrs }];
  expect(contents(privacyFilter({ [flag]: true }).span(span))).not.toContain("private-content");
});
test("S1 invocation parameters and image/file URIs have explicit hide policies", () => {
  const options = resolveConfig({}, { FI_HIDE_LLM_INVOCATION_PARAMETERS: "true", FI_HIDE_INPUT_IMAGES: "true" }).traceConfig;
  expect(options.hideLLMInvocationParameters).toBe(true);
  const attrs = { "gen_ai.request.parameters": "private-content", "gen_ai.request.temperature": 0.1, "gen_ai.tool.schema": "private-content", "gen_ai.input.messages.0.file.uri": "https://cdn/private-content", "input.value": "private-content" };
  expect(JSON.stringify(privacyFilter(options).attributes(attrs))).not.toContain("private-content");
});
test.each(["body", "nested-json", "multipart", "query"])("S1 retains %s credentials before upstream scrubbing for every export surface", source => {
  const secret = "request-private-token-12345", target = source === "query" ? url + "?%6bey=" + encodeURIComponent(secret) : url;
  const body = source === "body" ? { messages: [], mcp_servers: [{ authorization_token: secret }] } : source === "nested-json" ? { messages: [{ role: "user", content: JSON.stringify({ api_key: secret }) }] } : { messages: [] };
  const req = { method: "POST", url: target, headers: {}, body: Buffer.from(JSON.stringify(body)), ...(source === "multipart" ? { fields: { api_key: secret, messages: "[]" } } : {}) };
  const found = match("POST", target)!;
  // Multipart text is accepted by the audio endpoint, including arbitrary credentials.
  if (source === "multipart") { req.url = "https://api.openai.com/v1/audio/transcriptions"; req.fields = { model: "whisper-1", api_key: secret } as any; }
  const result = build(source === "multipart" ? match("POST", req.url)! : found, req, { status: 401, headers: { "content-type": "application/json" }, size: 0, body: Buffer.from(JSON.stringify({ error: { message: `Invalid credential ${secret}` } })) })!;
  expect(result).toBeDefined(); expect(JSON.stringify(result)).not.toContain(secret);
  expect(captureSecrets(result)).toContain(secret); expect(JSON.stringify(result)).not.toContain("request-private");
  const span = fake(result.attributes); span.status = { code: 2, message: secret };
  span.events = [{ name: secret, attributes: { "exception.stacktrace": secret } }]; span.resource.attributes = { echoed: secret };
  span.instrumentationScope = { name: secret, version: secret, attributes: { echoed: secret } };
  span.links = [{ context: { ...span._spanContext, traceState: createTraceState(`vendor=${secret}`) }, attributes: { echoed: secret } }];
  span._spanContext.traceState = createTraceState(`vendor=${secret}`);
  expect(contents(privacyFilter().span(span, captureSecrets(result)))).not.toContain(secret);
  expect(contents(span)).toContain(secret);
});

test("S1 request discovery exhaustion keeps a fail-closed span and cannot leak later-added content", () => {
  const request = { method: "POST", url, headers: {}, body: Buffer.from(JSON.stringify({ messages: [], extras: Array(20001).fill("request-private-token") })) };
  const result = build(match("POST", url)!, request, { status: 401, headers: {}, size: 0, body: Buffer.from('request-private-token') })!;
  expect(result).toBeDefined(); expect(captureSecrets(result)).toBeNull(); expect(result.attributes).toEqual({}); expect(result.description).toBeUndefined();
  const span = fake({ later: "request-private-token" }); span.status = { code: 2, message: "request-private-token" }; span.resource.attributes = { echoed: "request-private-token" };
  const output = privacyFilter().span(span, captureSecrets(result));
  expect(contents(output)).not.toContain("request-private-token"); expect(output.startTime).toEqual([1, 2]); expect(output.endTime).toEqual([3, 4]);
});

test.each(["string", "view", "array-buffer", "search-params"])("S1 truncated %s request discovery fails closed without changing response bytes", async kind => {
  const secret = "embedded-mcp-private-token-12345";
  const text = JSON.stringify({ messages: [], padding: "x".repeat(4194304), mcp_servers: [{ authorization_token: secret }] });
  const bytes = new TextEncoder().encode(text);
  const body = (kind === "view" ? bytes : kind === "array-buffer" ? bytes.buffer : kind === "search-params" ? new URLSearchParams({ payload: text }) : text) as BodyInit;
  const echoed = JSON.stringify({ error: { message: `Invalid MCP credential ${secret}` } });
  const response = new Response(echoed, { status: 401, headers: { "content-type": "application/json" } }), promise = Promise.resolve(response);
  const target = jest.fn((_input: RequestInfo | URL, _init?: RequestInit) => promise); globalThis.fetch = target;
  const emit = jest.fn(), h = createFetchHook(4194304, emit, { active: () => undefined, skip: () => false, run: (_p, f) => f() }); remove = h.remove;
  expect(fetch(url, { method: "POST", body })).toBe(promise); expect(await promise).toBe(response);
  expect(await response.text()).toBe(echoed); expect(target).toHaveBeenCalledTimes(1); expect(target.mock.calls[0][0]).toBe(url); expect(target.mock.calls[0][1]?.body).toBe(body);
  expect(emit).toHaveBeenCalledTimes(1);
  const result = emit.mock.calls[0][0]; expect(result.ok).toBe(false); expect(captureSecrets(result)).toBeNull();
  const span = fake({ later: secret }); span.status = { code: 2, message: secret }; span.events = [{ name: secret, attributes: { "exception.message": secret } }];
  span.resource.attributes = { echoed: secret }; span.instrumentationScope = { name: secret };
  const output = privacyFilter().span(span, captureSecrets(result));
  expect(contents(output)).not.toContain(secret); expect(output.status.code).toBe(2); expect(output.startTime).toEqual([1, 2]); expect(output.endTime).toEqual([3, 4]);
  expect(JSON.stringify(result)).not.toContain(secret); expect(contents(span)).toContain(secret);
});

test.each(["string", "view", "array-buffer", "search-params"])("S1 %s capture truncation below discovery budget retains credentials and safe metadata", async kind => {
  const secret = "small-cap-private-token-12345", text = JSON.stringify({ messages: [], mcp_servers: [{ authorization_token: secret }] });
  const bytes = new TextEncoder().encode(text), body = (kind === "view" ? bytes : kind === "array-buffer" ? bytes.buffer : kind === "search-params" ? new URLSearchParams({ payload: text }) : text) as BodyInit;
  globalThis.fetch = jest.fn(async () => new Response(JSON.stringify({ error: { message: `Invalid ${secret}` } }), { status: 401, headers: { "content-type": "application/json" } }));
  const emit = jest.fn(), h = createFetchHook(64, emit, { active: () => undefined, skip: () => false, run: (_p, f) => f() }); remove = h.remove;
  await (await fetch(url, { method: "POST", body })).text();
  expect(emit).toHaveBeenCalledTimes(1); const result = emit.mock.calls[0][0];
  expect(result.attributes["traceai.wire.truncated"]).toBe(true); expect(captureSecrets(result)).toContain(secret); expect(JSON.stringify(result)).not.toContain(secret);
});

test.each(["truncated", "duplicate", "over-budget"])("S1 multipart %s fields preserve credential discovery before capture disposal", async mode => {
  const secret = "multipart-private-token-12345", body = new FormData(); body.set("model", "whisper-1");
  body.append("settings", JSON.stringify({ padding: "x".repeat(mode === "over-budget" ? 4194304 : 65536), mcp: { authorization_token: secret } }));
  if (mode === "duplicate") body.append("settings", "ordinary settings");
  const echoed = JSON.stringify({ error: { message: `Invalid ${secret}` } });
  globalThis.fetch = jest.fn(async () => new Response(echoed, { status: 401, headers: { "content-type": "application/json" } }));
  const emit = jest.fn(); hook(emit);
  expect(await (await fetch("https://api.openai.com/v1/audio/transcriptions", { method: "POST", body })).text()).toBe(echoed);
  expect(emit).toHaveBeenCalledTimes(1); const result = emit.mock.calls[0][0];
  expect(result.ok).toBe(false); expect(JSON.stringify(result)).not.toContain(secret);
  if (mode === "over-budget") expect(captureSecrets(result)).toBeNull();
  else expect(captureSecrets(result)).toContain(secret);
});

test("S1 discovers signed URL secrets on forwarded spans before scrubbing other surfaces", () => {
  const token = "private+/token=12345", encoded = encodeURIComponent(token);
  const span = fake({ url: `https://api.test/?%6bey=${encoded}`, echoed: token }); span.status = { code: 2, message: `Invalid ${token}` };
  span.events = [{ name: encoded, attributes: { "exception.message": token } }];
  const output = contents(privacyFilter().span(span)); expect(output).not.toContain(token); expect(output).not.toContain(encoded); expect(contents(span)).toContain(token);
});

test("round3 generic Bedrock invoke vector fallback obeys hideEmbeddingVectors", () => {
  const target = "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke";
  const span = build(match("POST", target)!, { method: "POST", url: target, headers: {}, body: Buffer.from('{"inputText":"hello"}') }, { status: 200, headers: {}, size: 0, body: Buffer.from('{"embedding":[0.123,0.456],"inputTextTokenCount":3}') })!;
  const clean = privacyFilter({ hideEmbeddingVectors: true }).attributes(span.attributes);
  expect(JSON.stringify(clean)).not.toContain("0.123"); expect(clean["output.value"]).toBe("__REDACTED__");
  expect(privacyFilter({ hideEmbeddingVectors: true }).attributes({ vector: [0.123, 0.456] }).vector).toBe("__REDACTED__");
  const nested = JSON.stringify({ arguments: JSON.stringify({ embedding: [0.123] }) });
  expect(privacyFilter({ hideEmbeddingVectors: true }).attributes({ "output.value": nested })["output.value"]).toBe("__REDACTED__");
  expect(privacyFilter({ hideEmbeddingVectors: true }).attributes({ "gen_ai.operation.name": "embeddings", "gen_ai.embeddings.dimension.count": 3 })).toMatchObject({ "gen_ai.operation.name": "embeddings", "gen_ai.embeddings.dimension.count": 3 });
});
test.each([1, 5, 20])("S4 dedup traverses %i intermediate native spans, including a kind set after onStart", depth => {
  const processor = new SimpleSpanProcessor({ export(_s, cb) { cb({ code: 0 }); }, async shutdown() {} }); processor.onStart = trackCoverage;
  const provider = new BasicTracerProvider({ spanProcessors: [processor] }), tracer = provider.getTracer("native");
  const model = tracer.startSpan("model"); let parent = trace.setSpan(context.active(), model);
  for (let i = 0; i < depth; i++) parent = trace.setSpan(parent, tracer.startSpan("native", {}, parent));
  expect(shouldSkip(parent)).toBe(false); model.setAttribute("gen_ai.span.kind", "LLM"); expect(shouldSkip(parent)).toBe(true);
  model.end();
});
test.each(["", "}", "}}", '\\n at app (app.js:1:1)'])("round3 truncated nested audio never leaks (%s)", suffix => {
  const text = '400 {"audio":{"source":{"bytes":"U0VDUkVULUFVRElP"}' + suffix;
  expect(scrubText(text)).not.toContain("U0VDUkVULUFVRElP");
});
test.each([undefined, "response.failed", "response.incomplete"])("round3 interrupted Responses tool streams retain consumed function fragments (%s)", terminal => {
  const target = "https://api.openai.com/v1/responses", events = [
    { type: "response.output_item.added", output_index: 2, item: { type: "function_call", call_id: "call_1", name: "weather", arguments: "" } },
    { type: "response.function_call_arguments.delta", output_index: 2, delta: '{"city":' },
    { type: "response.function_call_arguments.delta", output_index: 2, delta: '"Oslo"}' },
    ...(terminal ? [{ type: terminal, response: { status: "incomplete", output: [] } }] : []),
  ];
  const span = build(match("POST", target)!, { method: "POST", url: target, headers: {}, body: Buffer.from('{"model":"m"}') }, { status: 200, headers: { "content-type": "text/event-stream" }, size: 0, incomplete: true, body: Buffer.from(events.map(e => `data: ${JSON.stringify(e)}\n\n`).join("")) })!;
  expect(span.attributes).toMatchObject({ "traceai.wire.incomplete": true, "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name": "weather", "gen_ai.output.messages.0.message.tool_calls.0.tool_call.id": "call_1", "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments": '{"city":"Oslo"}' });
});

let original: typeof fetch, remove: (() => void) | undefined;
beforeEach(() => { original = globalThis.fetch; });
afterEach(() => { remove?.(); remove = undefined; globalThis.fetch = original; });
function hook(emit: (...args: any[]) => void = () => {}) { const h = createFetchHook(4096, emit, { active: () => undefined, skip: () => false, run: (_p, f) => f() }); remove = h.remove; return h; }
test.each(["before", "after", "replace"])("R4-6 a broken context runner cannot retry or replace the transport result (%s)", async mode => {
  const response = new Response(answer), promise = Promise.resolve(response), target = jest.fn(() => promise);
  globalThis.fetch = target;
  const h = createFetchHook(4096, () => {}, { active: () => undefined, skip: () => false, run: (_p, work) => { if (mode === "before") throw new Error("context"); const result = work(); if (mode === "after") throw new Error("context"); return {} as typeof result; } }); remove = h.remove;
  expect(fetch(url, init)).toBe(promise); expect(await promise).toBe(response); await response.text(); expect(target).toHaveBeenCalledTimes(1);
});
test("R4-6 matched fetch returns original Promise/Response subclass and helper methods", async () => {
  class Custom extends Response { helper() { return this.status; } }
  const response = new Custom(answer), promise = Object.assign(Promise.resolve(response), { helper: () => response });
  globalThis.fetch = jest.fn(() => promise); const emit = jest.fn(); hook(emit);
  const got = fetch(url, init); expect(got).toBe(promise); expect((got as any).helper()).toBe(response);
  expect(await got).toBe(response); expect((await got as Custom).helper()).toBe(200);
  expect(await response.json()).toHaveProperty("choices"); expect(emit).toHaveBeenCalledTimes(1);
});
test.each(["text", "json", "arrayBuffer", "blob"] as const)("R4-6 original %s helper results and promises are retained", async method => {
  const response = new Response(answer), helper = response[method].bind(response) as any;
  let returned: any; Object.defineProperty(response, method, { value: function() { returned = helper(); return returned; } });
  globalThis.fetch = jest.fn(async () => response); const emit = jest.fn(); hook(emit);
  const got = await fetch(url, init), result = got[method](); expect(result).toBe(returned); await result;
  expect(got).toBe(response); expect(got.bodyUsed).toBe(true); expect(emit).toHaveBeenCalledTimes(1);
});
test.each([false, true])("R4-6 BYOB support and failures match baseline (byte source %s)", async bytes => {
  const make = () => bytes ? new Response(new ReadableStream({ type: "bytes", start(c) { c.enqueue(new TextEncoder().encode(answer)); c.close(); } })) : new Response(answer);
  const baseline = make(); let expectedError: any;
  try { baseline.body!.getReader({ mode: "byob" }); } catch (e) { expectedError = e; }
  globalThis.fetch = jest.fn(async () => make()); const emit = jest.fn(); hook(emit); const response = await fetch(url, init);
  if (expectedError) { expect(() => response.body!.getReader({ mode: "byob" })).toThrow(expectedError.message); expect(response.body!.locked).toBe(false); await response.text(); }
  else {
    const reader = response.body!.getReader({ mode: "byob" }), received: Buffer[] = [];
    for (;;) { const result = await reader.read(new Uint8Array(11)); if (result.done) break; received.push(Buffer.from(result.value)); }
    expect(Buffer.concat(received).toString()).toBe(answer);
  }
  expect(emit).toHaveBeenCalledTimes(1);
});
test("R4-6 clone/tee branch bytes and span count match an uninstrumented baseline", async () => {
  const run = async (response: Response) => { const clone = response.clone(), branches = response.body!.tee(); return Promise.all([clone.text(), ...branches.map(s => new Response(s).text())]); };
  const baseline = await run(new Response(answer)); const response = new Response(answer); globalThis.fetch = jest.fn(async () => response);
  const emit = jest.fn(); hook(emit); expect(await run(await fetch(url, init))).toEqual(baseline); expect(emit).toHaveBeenCalledTimes(1);
});
test.each(["pipeTo", "pipeThrough"])("R4-6 native %s retains stream/sink semantics with metadata-only completion", async mode => {
  const run = async (response: Response) => {
    const chunks: Uint8Array[] = [];
    if (mode === "pipeTo") await response.body!.pipeTo(new WritableStream({ write(chunk) { chunks.push(chunk); } }));
    else {
      const output = response.body!.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({ transform(chunk, c) { c.enqueue(chunk); } }));
      const reader = output.getReader(); for (;;) { const item = await reader.read(); if (item.done) break; chunks.push(item.value); }
    }
    return Buffer.concat(chunks).toString();
  };
  const baseline = await run(new Response(answer)), response = new Response(answer);
  globalThis.fetch = jest.fn(async () => response); const emit = jest.fn(); hook(emit);
  expect(await run(await fetch(url, init))).toBe(baseline); expect(emit).toHaveBeenCalledTimes(1);
  expect(emit.mock.calls[0][0].attributes).toMatchObject({ "traceai.wire.body_unavailable": true });
  expect(emit.mock.calls[0][0].attributes["output.value"]).toBeUndefined();
});
test("R4-6 cancellation preserves original promise/reason and never pulls an unread body", async () => {
  const reason = new Error("consumer"), pull = jest.fn(), cancel = jest.fn(() => Promise.resolve());
  const response = new Response(new ReadableStream({ pull, cancel }, { highWaterMark: 0 }));
  const native = response.body!.cancel.bind(response.body); let promise: any;
  Object.defineProperty(response.body, "cancel", { configurable: true, value: (value: unknown) => (promise = native(value)) });
  globalThis.fetch = jest.fn(async () => response); const emit = jest.fn(); const h = hook(emit);
  expect(await fetch(url, init)).toBe(response); await Promise.resolve();
  expect(pull).not.toHaveBeenCalled(); expect(response.bodyUsed).toBe(false); expect(response.body!.locked).toBe(false); expect(emit).not.toHaveBeenCalled(); expect(h.pending.size).toBe(0);
  const got = response.body!.cancel(reason); expect(got).toBe(promise); await got;
  expect(cancel).toHaveBeenCalledWith(reason); expect(pull).not.toHaveBeenCalled(); expect(emit).toHaveBeenCalledTimes(1);
});
test("R4-7 request inspection and slow parse cannot move transport start/end boundaries", async () => {
  const now = jest.spyOn(Date, "now"); let time = 100; now.mockImplementation(() => time);
  const response = new Response(answer, { headers: { "content-length": String(Buffer.byteLength(answer)) } });
  const initWithGetter = Object.defineProperty({ ...init }, "body", { get() { time += 500; return init.body; } });
  globalThis.fetch = jest.fn(() => { expect(time).toBe(100); return Promise.resolve(response); });
  const emit = jest.fn(); hook(emit);
  try {
    const got = await fetch(url, initWithGetter); time = 800;
    const native = JSON.parse; const parse = jest.spyOn(JSON, "parse").mockImplementation((...args: Parameters<typeof JSON.parse>) => { time += 1000; return native(...args); });
    try { await got.json(); } finally { parse.mockRestore(); }
    expect(emit).toHaveBeenCalledTimes(1); expect(emit.mock.calls[0][2]).toBe(100); expect(emit.mock.calls[0][3]).toBe(800);
  } finally { now.mockRestore(); }
});
