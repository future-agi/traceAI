import { gzipSync, deflateSync, brotliCompressSync } from "node:zlib";
import * as zlib from "node:zlib";
import { patchFetch } from "../src/wire/fetch";
import { SpanResult, build, loadCatalog, match } from "../src/wire/core";
const url = "https://api.openai.com/v1/chat/completions";
const body = JSON.stringify({ model: "gpt-test", messages: [{ role: "user", content: "hello" }] });
const init = { method: "POST", headers: { "content-type": "application/json" }, body };
const partial = 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n';
let original: typeof fetch, spans: SpanResult[], remove: (() => void) | undefined;
beforeEach(() => { original = globalThis.fetch; spans = []; });
afterEach(() => { remove?.(); globalThis.fetch = original; remove = undefined; });
function hook(max = 4194304, secrets: string[] = []) { const h = patchFetch(max, span => spans.push(span), secrets); remove = h.remove; return h; }
function response(text = '{"choices":[{"message":{"role":"assistant","content":"answer"}}]}') { return new Response(text, { headers: { "content-type": "application/json" } }); }
test.each([url, "https://gateway.example/v1/chat/completions"])("one-shot Request bodies are never cloned or read ahead (%s)", async target => {
  const request = new Request(target, init), clone = jest.spyOn(request, "clone");
  globalThis.fetch = jest.fn(async (input: any) => { expect(await input.text()).toBe(body); return response(); }); hook();
  await (await fetch(request)).text(); expect(clone).not.toHaveBeenCalled(); expect(request.bodyUsed).toBe(true);
  expect(spans).toHaveLength(target === url ? 1 : 0);
  if (spans.length) { expect(spans[0].attributes["traceai.wire.body_unavailable"]).toBe(true); expect(spans[0].attributes["input.value"]).toBeUndefined(); }
});
test("stream request transport failures capture known hosts only without pulling instrumentation reads", async () => {
  const failure = new TypeError("connection refused"), reader = jest.fn();
  const stream = new ReadableStream({ pull: reader }, { highWaterMark: 0 });
  globalThis.fetch = jest.fn(async () => { throw failure; }); hook();
  for (const target of [url, "https://gateway.example/v1/chat/completions"]) await expect(fetch(target, { ...init, body: stream, duplex: "half" } as any)).rejects.toBe(failure);
  expect(reader).not.toHaveBeenCalled(); expect(spans).toHaveLength(1);
  expect(spans[0]).toMatchObject({ ok: false, attributes: { "traceai.wire.body_unavailable": true, "error.type": "TypeError" } });
});
test("transport starts before materialized multipart fields are inspected", async () => {
  let sent = false; const form = new FormData(); form.set("model", "whisper-1"); form.set("prompt", "p".repeat(8192));
  form.set("file", new Blob([new Uint8Array(5 * 1024 * 1024)], { type: "audio/wav" }), "long.wav");
  const entries = form.entries.bind(form); jest.spyOn(form, "entries").mockImplementation(() => { expect(sent).toBe(true); return entries(); });
  globalThis.fetch = jest.fn(async () => { sent = true; return response('{"text":"transcript"}'); }); hook(32);
  await (await fetch("https://api.openai.com/v1/audio/transcriptions", { method: "POST", body: form })).text();
  expect(spans[0].name).toBe("transcription whisper-1"); expect(spans[0].attributes["traceai.audio.input_bytes"]).toBe(5 * 1024 * 1024);
  expect(JSON.parse(String(spans[0].attributes["input.value"])).prompt).toHaveLength(8192);
  expect(spans[0].attributes["traceai.wire.truncated"]).toBeUndefined();
});
test("multipart text fields have an independent 64 KiB limit", async () => {
  const form = new FormData(); form.set("model", "whisper-1"); form.set("prompt", "p".repeat(70000));
  globalThis.fetch = jest.fn(async () => response('{"text":"transcript"}')); hook(32);
  await (await fetch("https://api.openai.com/v1/audio/transcriptions", { method: "POST", body: form })).text();
  expect(JSON.parse(String(spans[0].attributes["input.value"])).prompt).toHaveLength(65536);
});
test("mid-stream failures preserve partial output and the original rejection", async () => {
  const failure = Object.assign(new Error("read timeout"), { name: "TimeoutError" }); let count = 0;
  globalThis.fetch = jest.fn(async () => new Response(new ReadableStream({ pull(c) { if (count++ === 0) c.enqueue(new TextEncoder().encode(partial)); else c.error(failure); } }, { highWaterMark: 0 }), { headers: { "content-type": "text/event-stream" } })); hook();
  const reader = (await fetch(url, init)).body!.getReader(); expect((await reader.read()).done).toBe(false);
  await expect(reader.read()).rejects.toBe(failure);
  expect(spans[0]).toMatchObject({ ok: false, description: "read timeout", attributes: { "output.value": "partial", "error.type": "TimeoutError" } });
});
test("a mid-stream transport failure also preserves output from incomplete gzip bytes", async () => {
  const full = gzipSync(partial), bytes = full.subarray(0, full.length - 8), failure = new TypeError("reset"); let count = 0;
  globalThis.fetch = jest.fn(async () => new Response(new ReadableStream({ pull(c) { if (count++ === 0) c.enqueue(new Uint8Array(bytes)); else c.error(failure); } }, { highWaterMark: 0 }), { headers: { "content-type": "text/event-stream", "content-encoding": "gzip" } })); const h = hook();
  const reader = (await fetch(url, init)).body!.getReader(); await reader.read(); await expect(reader.read()).rejects.toBe(failure); await Promise.all([...h.pending]);
  expect(spans[0]).toMatchObject({ ok: false, attributes: { "output.value": "partial", "error.type": "TypeError" } });
});
test("consumer cancellation is OK with partial output and incomplete marker", async () => {
  const cancel = jest.fn();
  globalThis.fetch = jest.fn(async () => new Response(new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(partial)); }, cancel }, { highWaterMark: 0 }), { headers: { "content-type": "text/event-stream" } })); hook();
  const reader = (await fetch(url, init)).body!.getReader(); await reader.read(); await reader.cancel("enough");
  expect(cancel).toHaveBeenCalledWith("enough"); expect(spans[0]).toMatchObject({ ok: true, attributes: { "output.value": "partial", "traceai.wire.incomplete": true } });
  expect(spans[0].attributes["error.type"]).toBeUndefined();
});
test.each([["gzip", gzipSync], ["deflate", deflateSync]] as const)("encoded %s responses decode before parsing but retain application bytes", async (encoding, compress) => {
  const bytes = compress(partial); globalThis.fetch = jest.fn(async () => new Response(bytes, { headers: { "content-type": "text/event-stream", "content-encoding": encoding } })); const h = hook();
  expect(Buffer.from(await (await fetch(url, init)).arrayBuffer())).toEqual(bytes); await Promise.all([...h.pending]);
  expect(spans[0].attributes["output.value"]).toBe("partial"); expect(spans[0].attributes["traceai.wire.undecoded_encoding"]).toBeUndefined();
});
test("Node Brotli and optional zstd decoders run whenever the runtime has them", async () => {
  const codecs: [string, (body: string) => Buffer][] = [["br", brotliCompressSync]];
  if (typeof (zlib as any).zstdCompressSync === "function") codecs.push(["zstd", (zlib as any).zstdCompressSync]);
  const mock = jest.fn(); globalThis.fetch = mock; const h = hook();
  for (const [encoding, compress] of codecs) {
    const bytes = compress(partial);
    mock.mockResolvedValueOnce(new Response(bytes, { headers: { "content-type": "text/event-stream", "content-encoding": encoding } }));
    expect(Buffer.from(await (await fetch(url, init)).arrayBuffer())).toEqual(bytes); await Promise.all([...h.pending]);
    expect(spans.at(-1)!.attributes["output.value"]).toBe("partial");
    expect(spans.at(-1)!.attributes["traceai.wire.undecoded_encoding"]).toBeUndefined();
  }
  expect(spans).toHaveLength(codecs.length);
});
test("already-decoded native-fetch bytes are never decoded a second time", async () => {
  globalThis.fetch = jest.fn(async () => new Response(partial, { headers: { "content-type": "text/event-stream", "content-encoding": "gzip" } })); hook();
  expect(await (await fetch(url, init)).text()).toBe(partial); expect(spans[0].attributes["output.value"]).toBe("partial");
});
test("native fetch response metadata prevents re-decoding plain-text transcription", async () => {
  const raw = new Response("plain transcript", { headers: { "content-type": "text/plain", "content-encoding": "br" } }); Object.defineProperty(raw, "type", { value: "basic" });
  globalThis.fetch = jest.fn(async () => raw); hook();
  const form = new FormData(); form.set("model", "whisper-1");
  expect(await (await fetch("https://api.openai.com/v1/audio/transcriptions", { method: "POST", body: form })).text()).toBe("plain transcript");
  expect(spans[0].attributes["gen_ai.audio.transcript"]).toBe("plain transcript");
});
test("decompressed output is bounded independently of compressed size", async () => {
  const bytes = gzipSync('data: {"choices":[{"delta":{"content":"' + "x".repeat(10000) + '"}}]}\n\n');
  globalThis.fetch = jest.fn(async () => new Response(bytes, { headers: { "content-type": "text/event-stream", "content-encoding": "gzip" } })); const h = hook(512);
  await (await fetch(url, init)).arrayBuffer(); await Promise.all([...h.pending]);
  expect(spans[0].attributes["traceai.wire.truncated"]).toBe(true); expect(spans[0].attributes["output.value"]).toBeUndefined();
});
test("unsupported response encodings keep request data and mark undecoded", async () => {
  globalThis.fetch = jest.fn(async () => new Response(new Uint8Array([255, 128, 129]), { headers: { "content-type": "application/json", "content-encoding": "unknown-codec" } })); const h = hook();
  await (await fetch(url, init)).arrayBuffer(); await Promise.all([...h.pending]);
  expect(spans[0].attributes["input.value"]).toBe(body); expect(spans[0].attributes["traceai.wire.undecoded_encoding"]).toBe("unknown-codec");
});
test("configured FI keys and request credentials are removed from every recorded string and error description", async () => {
  const api = "configured-api-secret", secret = "configured-secret-key", provider = "provider-header-secret";
  globalThis.fetch = jest.fn(async () => new Response(JSON.stringify({ error: { message: `${api} ${secret} ${provider}` } }), { status: 401, headers: { "content-type": "application/json" } })); hook(4194304, [api, secret]);
  await (await fetch(url, { ...init, headers: { ...init.headers, authorization: `Bearer ${provider}` }, body: JSON.stringify({ model: api, messages: [{ role: "user", content: secret }], custom: [provider] }) })).text();
  for (const value of [api, secret, provider]) expect(JSON.stringify(spans)).not.toContain(value);
  expect(spans[0].description).toBe("<redacted> <redacted> <redacted>");
});
test.each(["pooled Buffer", "reusable Uint8Array"])("response byte streams copy %s chunks without transferring the app's storage", async kind => {
  const bytes = kind === "pooled Buffer" ? Buffer.from('{"choices":[]}') : new TextEncoder().encode('{"choices":[]}');
  const expected = new Uint8Array(bytes), backing = bytes.buffer; let reads = 0;
  globalThis.fetch = jest.fn(async () => new Response(new ReadableStream({ pull(c) {
    if (reads++ === 0) c.enqueue(bytes); else { expect(new Uint8Array(bytes)).toEqual(expected); expect(backing.byteLength).toBeGreaterThan(0); c.close(); }
  } }, { highWaterMark: 0 }), { headers: { "content-type": "application/json" } })); hook();
  expect(await (await fetch(url, init)).text()).toBe('{"choices":[]}');
  expect(backing.byteLength).toBeGreaterThan(0); expect(new Uint8Array(bytes)).toEqual(expected); expect(spans).toHaveLength(1);
});
test.each(["text", "default-reader", "byob-reader"])("empty custom-fetch chunks preserve bytes and completion (%s)", async mode => {
  const text = '{"choices":[{"message":{"role":"assistant","content":"answer"}}]}';
  const chunks = [new Uint8Array(0), Buffer.from(text), new Uint8Array(0)];
  const makeResponse = () => {
    let index = 0;
    return new Response(new ReadableStream({ pull(c) { if (index < chunks.length) c.enqueue(chunks[index++]); else c.close(); } }, { highWaterMark: 0 }), { headers: { "content-type": "application/json" } });
  };
  globalThis.fetch = jest.fn(async () => makeResponse());
  expect(await (await fetch(url, init)).text()).toBe(text); hook();
  const observed = await fetch(url, init);
  if (mode === "byob-reader") {
    // A custom non-byte stream rejects BYOB without the agent too. Never upgrade it.
    const baseline = makeResponse();
    expect(() => baseline.body!.getReader({ mode: "byob" })).toThrow("must be a byte stream");
    expect(() => observed.body!.getReader({ mode: "byob" })).toThrow("must be a byte stream");
    expect(observed.body!.locked).toBe(false); expect(await observed.text()).toBe(text);
  } else if (mode === "text") expect(await observed.text()).toBe(text);
  else {
    const reader: any = mode === "byob-reader" ? observed.body!.getReader({ mode: "byob" }) : observed.body!.getReader();
    const received: Buffer[] = [];
    for (;;) {
      const item = await (mode === "byob-reader" ? reader.read(new Uint8Array(11)) : reader.read());
      if (item.done) break;
      received.push(Buffer.from(item.value));
    }
    expect(Buffer.concat(received).toString()).toBe(text);
  }
  expect(spans).toHaveLength(1); expect(spans[0]).toMatchObject({ ok: true, attributes: { "output.value": "answer" } });
  expect(spans[0].attributes["traceai.wire.incomplete"]).toBeUndefined();
});
test("cloning a mutable custom Response preserves metadata and independent header snapshots", async () => {
  const raw = response(); Object.defineProperty(raw, "url", { value: "https://api.openai.com/real" });
  globalThis.fetch = jest.fn(async () => raw); hook();
  const observed = await fetch(url, init); observed.headers.set("content-type", "text/custom");
  const clone = observed.clone(), second = clone.clone();
  const baseline = response(); Object.defineProperty(baseline, "url", { value: raw.url });
  const baselineClone = baseline.clone();
  expect([clone.url, second.url]).toEqual([baselineClone.url, baselineClone.clone().url]);
  expect(observed).toBe(raw);
  expect(clone.headers.get("content-type")).toBe("text/custom");
  clone.headers.set("clone-only", "yes"); expect(observed.headers.has("clone-only")).toBe(false); expect(second.headers.has("clone-only")).toBe(false);
  expect(await clone.text()).toContain("answer"); expect(await second.text()).toContain("answer");
  const blob = await observed.blob(); expect(blob.type).toBe("text/custom"); expect(await blob.text()).toContain("answer"); expect(spans).toHaveLength(1);
});
test.each([["gzip", gzipSync], ["deflate", deflateSync], ["br", brotliCompressSync]] as const)("%s ElevenLabs JSON audio decodes before base64 counting", async (encoding, compress) => {
  const text = JSON.stringify({ audio_base64: Buffer.alloc(128).toString("base64") }), bytes = compress(text);
  globalThis.fetch = jest.fn(async () => new Response(bytes, { headers: { "content-type": "application/json", "content-encoding": encoding } })); const h = hook();
  const target = "https://api.elevenlabs.io/v1/text-to-speech/voice/with-timestamps";
  expect(Buffer.from(await (await fetch(target, { ...init, body: '{"text":"hello","model_id":"tts"}' })).arrayBuffer())).toEqual(bytes);
  await Promise.all([...h.pending]); expect(spans).toHaveLength(1);
  expect(spans[0].attributes["traceai.audio.output_bytes"]).toBe(128); expect(spans[0].attributes["traceai.wire.undecoded_encoding"]).toBeUndefined();
  expect(JSON.stringify(spans)).not.toContain("audio_base64");
});
test("failure to construct an observed Response leaves the original body unlocked and readable", async () => {
  const raw = response(); Object.defineProperty(raw, "status", { value: 700 });
  globalThis.fetch = jest.fn(async () => raw); hook();
  const observed = await fetch(url, init); expect(observed).toBe(raw); expect(observed.body!.locked).toBe(false);
  expect(await observed.text()).toContain("answer");
});
test.each([[null], [[]], [42], ["text"]])("Bedrock invoke accepts any parseable JSON body (%j)", value => {
  const target = "https://bedrock-runtime.us-east-1.amazonaws.com/model/m/invoke";
  const result = build(match("POST", target)!, { method: "POST", url: target, headers: {}, body: Buffer.from(JSON.stringify(value)) }, { status: 200, headers: {}, body: Buffer.from("null"), size: 4 });
  expect(result?.name).toBe("chat m"); expect(result?.attributes["input.value"]).toBe(JSON.stringify(value)); expect(result?.attributes["output.value"]).toBe("null");
});
test("completed-only polling drops malformed/processing responses and retains explicit error completions", () => {
  const target = "https://api.assemblyai.com/v2/transcript/id";
  const request = { method: "GET", url: target, headers: {} }, found = match("GET", target)!;
  for (const text of ["not-json", "{}", '{"status":"processing"}']) expect(build(found, request, { status: 200, headers: {}, body: Buffer.from(text), size: text.length })).toBeUndefined();
  const text = '{"status":"error","error":"failed"}';
  expect(build(found, request, { status: 500, headers: {}, body: Buffer.from(text), size: text.length })).toMatchObject({ ok: false, description: "failed" });
});
test("exact catalog hosts take priority over a generic Ollama port", () => {
  expect(match("POST", "https://api.deepgram.com:11434/v1/listen")?.provider).toBe("deepgram");
  expect(match("POST", "https://bedrock-runtime.fake.example/model/m/converse")).toBeUndefined();
  expect(loadCatalog().providers.length).toBeGreaterThan(30);
});
