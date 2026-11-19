import { context, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { patchFetch } from "../src/wire/fetch";
import { SpanResult, build, match } from "../src/wire/core";
const url = "https://api.openai.com/v1/chat/completions";
const init = { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ model: "gpt-test", messages: [{ role: "user", content: "hello" }] }) };
const answer = JSON.stringify({ model: "gpt-test", choices: [{ message: { role: "assistant", content: "world" }, finish_reason: "stop" }] });
let original: typeof fetch, spans: SpanResult[], remove: (() => void) | undefined;
beforeAll(() => { context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable()); });
afterAll(() => { context.disable(); });
beforeEach(() => { original = globalThis.fetch; spans = []; });
afterEach(() => { remove?.(); remove = undefined; globalThis.fetch = original; });
function hook(max = 4194304) { const h = patchFetch(max, s => spans.push(s)); remove = h.remove; return h; }
function fakeResponse(body: BodyInit = answer, type = "application/json") { return new Response(body, { headers: { "content-type": type } }); }
test("fetch Proxy preserves name, length, custom properties and receiver", async () => {
  const mock = jest.fn(function(this: any) { expect(this).toBe(globalThis); return Promise.resolve(fakeResponse()); });
  Object.defineProperty(mock, "marker", { value: 42 }); globalThis.fetch = mock as any;
  hook(); expect(fetch.name).toBe(mock.name); expect(fetch.length).toBe(mock.length); expect((fetch as any).marker).toBe(42);
  const response = await globalThis.fetch(url, init); expect(await response.text()).toBe(answer); expect(spans).toHaveLength(1);
});
test("nested asynchronous captures produce only the outer span", async () => {
  let nested = false;
  const mock = jest.fn(async () => {
    if (!nested) { nested = true; await Promise.resolve(); await (await fetch(url, init)).text(); }
    return fakeResponse();
  }); globalThis.fetch = mock; hook(); await (await fetch(url, init)).text();
  expect(mock).toHaveBeenCalledTimes(2); expect(spans).toHaveLength(1);
});
test("synchronous host exceptions propagate once, without retries on skipped or matched traffic", () => {
  const error = new TypeError("host failure"), mock = jest.fn(() => { throw error; }); globalThis.fetch = mock; hook();
  expect(() => fetch("https://example.org/not-ai")).toThrow(error); expect(mock).toHaveBeenCalledTimes(1);
  expect(() => fetch(url, init)).toThrow(error); expect(mock).toHaveBeenCalledTimes(2); expect(spans[0].attributes["error.type"]).toBe("TypeError");
});
test("request and response limits keep the span, drop content and preserve application bytes", async () => {
  globalThis.fetch = jest.fn(async () => fakeResponse()); hook(24);
  expect(await (await fetch(url, init)).text()).toBe(answer);
  expect(spans).toHaveLength(1); expect(spans[0].attributes["traceai.wire.truncated"]).toBe(true);
  expect(spans[0].attributes["input.value"]).toBeUndefined(); expect(spans[0].attributes["output.value"]).toBeUndefined();
});
test("request object JSON capture is concurrent, bounded, and leaves bodyUsed semantics intact", async () => {
  globalThis.fetch = jest.fn(async (input: any) => { await input.text(); return fakeResponse(); }); const h = hook();
  const request = new Request(url, init); const response = await fetch(request); await response.text(); await Promise.all([...h.pending]);
  expect(request.bodyUsed).toBe(true); expect(spans).toHaveLength(1); expect(spans[0].name).toBe("chat gpt-test");
});
test("binary responses count bytes and support BYOB without recording audio", async () => {
  const bytes = new Uint8Array(1024).fill(39);
  globalThis.fetch = jest.fn(async () => fakeResponse(bytes, "audio/wav")); hook();
  const response = await fetch("https://api.openai.com/v1/audio/speech", { ...init, body: JSON.stringify({ model: "tts", input: "hello", voice: "alloy" }) });
  const reader = response.body!.getReader({ mode: "byob" });
  const got: number[] = [];
  while (true) { const item = await reader.read(new Uint8Array(137)); if (item.done) break; got.push(...item.value); }
  expect(got).toEqual(Array.from(bytes)); expect(spans[0].attributes["traceai.audio.output_bytes"]).toBe(1024);
  expect(spans[0].attributes["output.value"]).toBeUndefined();
});
test("FormData records file metadata without reading audio bytes", async () => {
  globalThis.fetch = jest.fn(async () => fakeResponse(JSON.stringify({ text: "transcript" }))); hook();
  const body = new FormData(); body.set("model", "whisper-1"); body.set("file", new Blob([new Uint8Array(31)], { type: "audio/wav" }), "audio.wav");
  await (await fetch("https://api.openai.com/v1/audio/transcriptions", { method: "POST", body })).text();
  expect(spans[0].attributes["traceai.audio.input_bytes"]).toBe(31); expect(spans[0].attributes["gen_ai.audio.mime_type"]).toBe("audio/wav");
  expect(spans[0].attributes["input.value"]).toBe(JSON.stringify({ model: "whisper-1", file: { filename: "audio.wav", content_type: "audio/wav", input_bytes: 31 } }));
});
test("binary Blob requests expose only size and MIME type without reading the Blob", async () => {
  globalThis.fetch = jest.fn(async () => fakeResponse(JSON.stringify({ results: { channels: [{ alternatives: [{ transcript: "words" }] }] } })));
  hook(); const blob = new Blob([new Uint8Array(512)], { type: "audio/wav" });
  const read = jest.spyOn(blob, "arrayBuffer");
  await (await fetch("https://api.deepgram.com/v1/listen?model=nova-2", { method: "POST", body: blob })).text();
  expect(read).not.toHaveBeenCalled(); expect(spans[0].attributes["traceai.audio.input_bytes"]).toBe(512);
});
test("audio endpoint HTTP errors retain the provider error message", async () => {
  globalThis.fetch = jest.fn(async () => new Response('{"error":{"message":"unauthorized"}}', { status: 401, headers: { "content-type": "application/json" } }));
  hook(); await (await fetch("https://api.openai.com/v1/audio/speech", { ...init, body: JSON.stringify({ input: "hi" }) })).text();
  expect(spans[0]).toMatchObject({ ok: false, description: "unauthorized" });
});
test("encoded audio JSON/SSE is counted across split keys without buffering base64", async () => {
  const encoded = Buffer.alloc(128, 42).toString("base64"), raw = JSON.stringify({ audio_base64: encoded, alignment: {} });
  globalThis.fetch = jest.fn(async () => fakeResponse(new ReadableStream({ start(c) { for (let i = 0; i < raw.length; i += 3) c.enqueue(new TextEncoder().encode(raw.slice(i, i + 3))); c.close(); } })));
  hook(8);
  const response = await fetch("https://api.elevenlabs.io/v1/text-to-speech/voice/with-timestamps", { ...init, body: JSON.stringify({ text: "hi" }) });
  expect(await response.text()).toBe(raw); expect(spans[0].attributes["traceai.audio.output_bytes"]).toBe(128);
  expect(JSON.stringify(spans)).not.toContain(encoded);
});
test("http errors and transport errors preserve the original application outcome", async () => {
  globalThis.fetch = jest.fn(async () => new Response('{"error":{"message":"unauthorized"}}', { status: 401, headers: { "content-type": "application/json" } })); hook();
  const response = await fetch(url, init); expect(response.status).toBe(401); await response.text();
  expect(spans[0]).toMatchObject({ ok: false, description: "unauthorized" });
});
test("remaining catalog formats parse completions, images, moderation, embed and invoke", () => {
  const cases = [
    ["https://api.openai.com/v1/completions", { model: "m", prompt: "hi" }, { choices: [{ text: "text" }] }, "output.value", "text"],
    ["https://api.openai.com/v1/images/generations", { prompt: "image" }, { data: [{ b64_json: "secret" }] }, "output.value", '{"data":[{"b64_json":"<base64 omitted>"}]}'],
    ["https://api.openai.com/v1/moderations", { input: "text" }, { results: [{ flagged: true }] }, "traceai.moderation.flagged", true],
    ["https://api.cohere.com/v2/embed", { texts: ["hi"] }, { embeddings: { float: [[1, 2, 3]] } }, "gen_ai.embeddings.dimension.count", 3],
    ["https://generativelanguage.googleapis.com/v1/models/m:embedContent", { content: {} }, { embedding: { values: [1, 2] } }, "gen_ai.embeddings.dimension.count", 2],
    ["http://localhost:11434/api/embed", { input: ["hi"] }, { embeddings: [[1, 2]] }, "gen_ai.embeddings.dimension.count", 2],
    ["http://localhost:11434/api/generate", { prompt: "hi", stream: false }, { response: "text", done: true }, "output.value", "text"],
    ["https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke", { prompt: "hi" }, { completion: "text" }, "output.value", '{"completion":"text"}'],
  ];
  for (const [url, input, output, key, value] of cases as any[]) {
    const result = build(match("POST", url)!, { method: "POST", url, headers: { "content-type": "application/json" }, body: Buffer.from(JSON.stringify(input)) }, { status: 200, headers: { "content-type": "application/json" }, body: Buffer.from(JSON.stringify(output)), size: 0 });
    expect(result?.attributes[key]).toEqual(value);
  }
});
