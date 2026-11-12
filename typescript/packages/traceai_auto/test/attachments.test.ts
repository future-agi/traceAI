import { createServer, request as httpRequest, type Server } from "node:http";
import { AddressInfo } from "node:net";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { context, trace, ROOT_CONTEXT, INVALID_SPAN_CONTEXT } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { AlwaysOffSampler, BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { AttachmentManager, createAttachmentHttpTransport, type AttachmentHttpClient, type AttachmentManagerOptions } from "@traceai/fi-core/attachments";
import { captureOpenAIAttachments } from "@traceai/openai";
import { patchFetch } from "../src/wire/fetch";
import { withAttachmentOwnership, withAttachmentCapturePolicy } from "../src/wire/attachments";
import { attachmentOptions, selectAttachmentManager, attachmentReport } from "../src/attachments";
import { resolveConfig } from "../src/config";

// Synthetic media/credentials only. Provider and attachment traffic both terminate on loopback.
const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64");
function wav(samples = 80) {
  const b = Buffer.alloc(44 + samples * 2); b.write("RIFF"); b.writeUInt32LE(b.length - 8, 4); b.write("WAVEfmt ", 8);
  b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22); b.writeUInt32LE(8000, 24);
  b.writeUInt32LE(16000, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write("data", 36); b.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i++) b.writeInt16LE(Math.round(8000 * Math.sin(i / 3)), 44 + i * 2);
  return b;
}
const audio = wav();
let original: typeof fetch, server: Server, origin: string, manager: AttachmentManager, hook: ReturnType<typeof patchFetch> | undefined;
let uploads: Buffer[], requests: any[], transfers: string[], providerBodies: Buffer[], providerRequests: number;
let provider: BasicTracerProvider, exporter: InMemorySpanExporter;
let responseStatus: number, responseData: Buffer, responseType: string, responseChunks: number;
const nativeFetch = globalThis.fetch;
const serverPolicy = { version: "fixture-v1", enabled: true, allowed_sources: ["Bytes", "Blob"], allowed_modalities: ["image", "audio", "document", "file"],
  require_media_redaction: false, require_metadata_removal: false, allow_unredacted_media_with_text_pii: false, record_audio: false, record_video: false };
const limits = { max_upload_bytes: 20971520, segment_bytes: 8388608, segment_duration_ms: 30000, recording_bytes: 1073741824, recording_duration_ms: 7200000,
  active_transfers: 16, active_recordings: 4, reserved_bytes: 1073741824, validation_jobs: 1024, initializations_per_minute: 120, links_per_minute: 1000,
  upload_bytes_per_second: 1073741824, playback_bytes_per_second: 1073741824, transfer_timeout_seconds: 600, grant_ttl_seconds: 300, revocation_check_seconds: 5 };
let policy: Record<string, unknown> = serverPolicy;
const scope = { organization_id: randomUUID(), workspace_id: randomUUID(), project_id: randomUUID() };
// Sessions of one hour or more: the core renews within max(60 s, 20% of lifetime) of expiry.
const expiry = () => new Date(Date.now() + 3600000).toISOString();
function control(req: import("node:http").IncomingMessage, body: Buffer, res: import("node:http").ServerResponse) {
  transfers.push(req.url!); res.setHeader("content-type", "application/json");
  if (req.url!.endsWith("attachment-sessions")) {
    const parsed = JSON.parse(body.toString());
    res.end(JSON.stringify({ schema_version: 1, scope, capture_session_id: parsed.capture_session_id, policy, limits, expires_at: expiry(), transfer_modes: ["authenticated_stream"] }));
  } else if (req.url!.endsWith("/uploads")) {
    const parsed = JSON.parse(body.toString()); requests.push(parsed); res.statusCode = 201;
    res.end(JSON.stringify({ upload_id: parsed.attachment_id, attachment_id: parsed.attachment_id, occurrence_id: parsed.occurrence_id, state: "pending_upload",
      reserved_bytes: parsed.declared_bytes, expires_at: expiry(), transfer_mode: "authenticated_stream", write_capability: "synthetic-upload-only" }));
  } else if (req.url!.endsWith("/content")) {
    uploads.push(body); res.end(JSON.stringify({ upload_id: req.url!.split("/").at(-2), state: "pending_upload", reason: "queued" }));
  } else { res.statusCode = 202; res.end(JSON.stringify({ upload_id: req.url!.split("/").at(-2), state: "validating", reason: "binding_pending" })); }
}
const client: AttachmentHttpClient = async req => {
  // Content PUTs may carry the application's own Blob; the double reads it only when the core sends it.
  const body = req.body instanceof Blob ? Buffer.from(await req.body.arrayBuffer()) : req.body;
  return new Promise((resolve, reject) => {
  const outgoing = httpRequest(`${origin}${new URL(req.url).pathname}`, { method: req.method, headers: { ...req.headers, "x-attachment-control": "1" }, signal: req.signal }, response => {
    const chunks: Buffer[] = []; response.on("data", c => chunks.push(c)); response.on("end", () => resolve({ status: response.statusCode!, headers: {}, body: Buffer.concat(chunks) } as any));
  }); outgoing.on("error", reject); outgoing.end(body as any);
  });
};
async function createManager(options: AttachmentManagerOptions = {}, serverOverrides: Record<string, unknown> = {}) {
  policy = { ...serverPolicy, ...serverOverrides };
  const result = new AttachmentManager({ enabled: true, projectName: "attachment-test", projectType: "observe", environment: () => ({}),
    transport: createAttachmentHttpTransport({ endpoint: "https://attachments.example", headers: { authorization: "Bearer synthetic" }, client }), ...options });
  expect(await result.ready()).toBe(options.enabled !== false);
  expect(result.status().state).toBe(options.enabled === false ? "disabled" : "ready");
  return result;
}
/**
 * `emit: "none"` models an emitter that never starts the wire span (for example its delivery gate refused it);
 * `"invalid"` binds a span with an invalid identity, which a deferred target refuses to bind.
 */
function install(target = manager, emit: "span" | "none" | "invalid" = "span") {
  globalThis.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const u = new URL(input instanceof Request ? input.url : String(input));
    return nativeFetch(`${origin}${u.pathname}${u.search}`, init);
  };
  hook = patchFetch(4194304, (result, parent, start, end, bindMedia) => {
    if (emit === "none") return;
    const span = emit === "invalid" ? trace.wrapSpanContext(INVALID_SPAN_CONTEXT) : provider.getTracer("wire-test").startSpan(result.name, { startTime: start, attributes: result.attributes }, parent);
    bindMedia?.(span); span.end(end);
  }, [], target);
}
const transcription = () => {
  const body = new FormData(); body.set("model", "whisper-1"); body.set("file", new Blob([audio], { type: "audio/wav" }), "PRIVATE_CANARY_FILENAME.wav");
  return fetch("https://api.openai.com/v1/audio/transcriptions", { method: "POST", body });
};
async function readAll(response: Response, mode: "reader" | "iterator" = "reader"): Promise<Buffer> {
  const chunks: Buffer[] = [];
  if (mode === "iterator") { for await (const chunk of response.body as any) chunks.push(Buffer.from(chunk)); }
  else { const reader = response.body!.getReader(); for (;;) { const x = await reader.read(); if (x.done) break; chunks.push(Buffer.from(x.value)); } }
  return Buffer.concat(chunks);
}
let nativeSpanId = "";
function inNative<T>(fn: () => T, kind = "LLM", policyContext = true): T {
  const span = provider.getTracer("native-test").startSpan("native", { attributes: { "gen_ai.span.kind": kind } });
  nativeSpanId = span.spanContext().spanId;
  const parent = trace.setSpan(ROOT_CONTEXT, span);
  const result = context.with(policyContext ? withAttachmentCapturePolicy(parent, { captureContent: true }) : parent, fn);
  // Promise work retains this exact request context; do not consult later ambient spans.
  if (result instanceof Promise) return result.finally(() => span.end()) as T;
  span.end(); return result;
}
async function settle() { await manager.flush(2000); }
const speech = (route = "api.openai.com/v1/audio/speech") => fetch(`https://${route}`, { method: "POST", body: JSON.stringify({ model: "tts-1", input: "synthetic", voice: "alloy" }) });
beforeAll(() => context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable()));
afterAll(() => context.disable());
beforeEach(async () => {
  original = globalThis.fetch; uploads = []; requests = []; transfers = []; providerBodies = []; providerRequests = 0;
  responseStatus = 200; responseData = audio; responseType = "audio/wav"; responseChunks = 1;
  server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => {
      const body = Buffer.concat(chunks);
      if (req.headers["x-attachment-control"]) { control(req, body, res); return; }
      providerRequests++; providerBodies.push(body);
      res.writeHead(responseStatus, { "content-type": responseType });
      // Several network chunks, spaced so loopback does not coalesce them: incremental readers see several reads.
      const size = Math.ceil(responseData.length / responseChunks), data = responseData;
      void (async () => {
        for (let i = 0; i < data.length; i += size) { res.write(data.subarray(i, i + size)); if (responseChunks > 1) await new Promise(r => setTimeout(r, 15)); }
        res.end();
      })();
    });
  });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done)); origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  exporter = new InMemorySpanExporter(); provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  manager = await createManager();
});
afterEach(async () => {
  hook?.remove(); hook = undefined; globalThis.fetch = original;
  await manager.shutdown(1000); await provider.shutdown();
  server.closeAllConnections(); await new Promise<void>(done => server.close(() => done()));
});
const sessionsOnly = () => transfers.filter(path => !path.endsWith("attachment-sessions"));

describe("A04 speech output", () => {
  test.each(["arrayBuffer", "blob"] as const)("wire-only: application-materialized %s uploads once, bound to the actual wire span", async method => {
    install(); const response = await speech();
    const result = await response[method]();
    expect(Buffer.from(result instanceof Blob ? await result.arrayBuffer() : result)).toEqual(audio);
    await settle(); expect(uploads).toEqual([audio]);
    const span = exporter.getFinishedSpans()[0];
    expect(requests[0].association.parent.span_id).toBe(span.spanContext().spanId);
    expect(requests[0]).toMatchObject({ direction: "output", purpose: "model_output", mime_type: "audio/wav", declared_bytes: audio.length });
    // The body is consumed through a reader the runtime opens, so the bytes arrive through a deferred target that binds
    // inside the emitter, before the wire span ends: the reference is projected onto that span, once.
    const refs = JSON.parse(String(span.attributes["fi.attachment.refs"]));
    expect(refs).toEqual([expect.objectContaining({ occurrence_id: requests[0].occurrence_id, modality: "audio", direction: "output" })]);
    expect(manager.status()).toMatchObject({ openTickets: 0, openStreams: 0, ownedBytes: 0 });
  });
  test("native span + explicit policy: arrayBuffer is captured once from consumed chunks, with no second span", async () => {
    responseChunks = 4; install();
    await inNative(async () => { await (await speech()).arrayBuffer(); });
    await settle(); expect(uploads).toEqual([audio]); expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(requests[0].association.parent.span_id).toBe(nativeSpanId);
    const refs = JSON.parse(String(exporter.getFinishedSpans()[0].attributes["fi.attachment.refs"]));
    expect(refs).toEqual([expect.objectContaining({ occurrence_id: requests[0].occurrence_id, modality: "audio", direction: "output" })]);
  });
  test("T-CS-01 native span: application reader consumption is observed with no extra read, clone or tee", async () => {
    responseChunks = 5; install();
    const tee = jest.spyOn(ReadableStream.prototype, "tee"), clone = jest.spyOn(Response.prototype, "clone");
    const reads = jest.spyOn(ReadableStreamDefaultReader.prototype, "read");
    let appReads = 0, responseBody: unknown, appReader: unknown;
    await inNative(async () => {
      const response = await speech(); responseBody = response.body; const reader = response.body!.getReader(); appReader = reader; const chunks: Buffer[] = [];
      for (;;) { appReads++; const x = await reader.read(); if (x.done) break; chunks.push(Buffer.from(x.value)); }
      expect(Buffer.concat(chunks)).toEqual(audio);
    });
    // Only this reader's calls: undici reads its own request-body stream internally.
    const underlying = reads.mock.contexts.filter(receiver => receiver === appReader).length;
    await settle();
    // undici itself tees the REQUEST body (cloneRequest); the response body must never be teed or cloned.
    expect(tee.mock.contexts).not.toContain(responseBody); expect(clone).not.toHaveBeenCalled(); tee.mockRestore(); clone.mockRestore(); reads.mockRestore();
    expect(appReads).toBeGreaterThan(2); expect(underlying).toBe(appReads); expect(uploads).toEqual([audio]);
  });
  test("native span: async-iterator consumption uploads once; a break before the end uploads nothing", async () => {
    responseChunks = 4; install();
    await inNative(async () => {
      const chunks: Buffer[] = []; for await (const chunk of (await speech()).body as any) chunks.push(Buffer.from(chunk));
      expect(Buffer.concat(chunks)).toEqual(audio);
    });
    await settle(); expect(uploads).toEqual([audio]);
    uploads = [];
    await inNative(async () => { for await (const _chunk of (await speech()).body as any) break; });
    await settle(); expect(uploads).toHaveLength(0); expect(manager.status()).toMatchObject({ openStreams: 0, ownedBytes: 0 });
  });
  test("adapter media ownership is independent and prevents duplicate uploads", async () => {
    install(); await inNative(() => context.with(withAttachmentOwnership(context.active()), async () => { await (await speech()).arrayBuffer(); }));
    await settle(); expect(uploads).toHaveLength(0); expect(exporter.getFinishedSpans()).toHaveLength(1);
  });
  test("the ElevenLabs marker (Symbol.for key, no auto import) is honoured", async () => {
    install(); await inNative(() => context.with(context.active().setValue(Symbol.for("traceai.auto.attachment_owner.v1"), true), async () => { await (await speech()).arrayBuffer(); }));
    await settle(); expect(uploads).toHaveLength(0);
  });
  test.each(["api.openai.com/v1/audio/speech", "api.deepgram.com/v1/speak"])("recognized %s: cloned application reads upload once and stay byte-identical", async route => {
    install(); await inNative(async () => {
      const response = await speech(route); const copy = response.clone();
      const [one, two] = await Promise.all([response.arrayBuffer(), copy.blob()]);
      expect(Buffer.from(one)).toEqual(audio); expect(Buffer.from(await two.arrayBuffer())).toEqual(audio);
    }); await settle(); expect(uploads).toEqual([audio]);
  });
  test("unread, cancelled and cancelled-midway bodies cause no eager read and upload nothing", async () => {
    responseChunks = 4; install();
    await inNative(async () => {
      const unread = await speech(); expect(unread.bodyUsed).toBe(false); await unread.body!.cancel();
      const partial = await speech(); const reader = partial.body!.getReader(); await reader.read(); await reader.cancel();
    });
    // Wire-only (deferred target): a reader cancelled midway uploads nothing, and nothing stays held.
    const wireOnly = await speech(); const reader = wireOnly.body!.getReader(); await reader.read(); await reader.cancel();
    await settle(); expect(uploads).toHaveLength(0); expect(manager.status()).toMatchObject({ openStreams: 0, openTickets: 0, ownedBytes: 0 });
  });
  test.each([
    ["api.openai.com/v1/audio/speech", { model: "tts-1", input: "synthetic", voice: "alloy" }, "reader"],
    ["api.openai.com/v1/audio/speech", { model: "tts-1", input: "synthetic", voice: "alloy" }, "iterator"],
    ["api.deepgram.com/v1/speak", { text: "synthetic" }, "reader"],
  ] as const)("wire-only %s (%j, %s): incremental reads upload once, bound to the wire span through a deferred target", async (route, body, mode) => {
    responseChunks = 4; install();
    const response = await fetch(`https://${route}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    expect(await readAll(response, mode)).toEqual(audio);
    await settle(); expect(uploads).toEqual([audio]);
    const spans = exporter.getFinishedSpans(); expect(spans).toHaveLength(1);
    expect(requests).toHaveLength(1);
    expect(requests[0].association.parent.span_id).toBe(spans[0].spanContext().spanId);
    expect(requests[0]).toMatchObject({ direction: "output", purpose: "model_output", mime_type: "audio/wav", declared_bytes: audio.length });
    expect(manager.status()).toMatchObject({ openStreams: 0, openTickets: 0, ownedBytes: 0 });
  });
  test.each(["none", "invalid"] as const)("wire-only captures whose wire span is never bound (emit: %s) are dropped when the call completes, not after the deadline", async emit => {
    responseType = "application/json"; responseData = Buffer.from('{"text":"synthetic"}'); install(manager, emit);
    await (await transcription()).json();
    responseType = "audio/wav"; responseData = audio; responseChunks = 4;
    expect(await readAll(await speech())).toEqual(audio);
    await new Promise(done => setImmediate(done));
    expect(manager.status()).toMatchObject({ openStreams: 0, openTickets: 0, ownedBytes: 0 });
    await settle(); expect(uploads).toHaveLength(0); expect(sessionsOnly()).toEqual([]); expect(exporter.getFinishedSpans()).toHaveLength(0);
  });
  test("piped bodies are not captured", async () => {
    install(); await inNative(async () => {
      const response = await speech(); const chunks: Uint8Array[] = [];
      await response.body!.pipeTo(new WritableStream({ write(chunk) { chunks.push(chunk); } }));
      expect(Buffer.concat(chunks)).toEqual(audio);
    }); await settle(); expect(uploads).toHaveLength(0);
  });
  test("an object above the stream cap is discarded whole (too_large), never truncated", async () => {
    responseData = Buffer.concat([audio, Buffer.alloc(4 * 1024 * 1024)]); responseChunks = 8; install();
    await inNative(async () => { const reader = (await speech()).body!.getReader(); while (!(await reader.read()).done) { /* app reads */ } });
    await settle(); expect(uploads).toHaveLength(0); expect(manager.status().ownedBytes).toBe(0);
  });
});

describe("A03 FormData and JSON media", () => {
  test("native: transcription FormData keeps body, boundary and filename on the provider path only", async () => {
    responseType = "application/json"; responseData = Buffer.from('{"text":"synthetic"}');
    const body = new FormData(); body.set("model", "whisper-1"); body.set("file", new Blob([audio], { type: "audio/wav" }), "PRIVATE_CANARY_FILENAME.wav");
    install(); await inNative(async () => { await (await fetch("https://api.openai.com/v1/audio/transcriptions", { method: "POST", body })).json(); });
    await settle(); expect(uploads).toEqual([audio]); expect(providerBodies[0].includes(audio)).toBe(true); expect(providerBodies[0].toString()).toContain("PRIVATE_CANARY_FILENAME.wav");
    expect(requests[0]).toMatchObject({ direction: "input", purpose: "model_input", source_kind: "Blob" });
    expect(JSON.stringify(requests)).not.toContain("PRIVATE_CANARY"); expect(JSON.stringify(exporter.getFinishedSpans().map(s => s.attributes))).not.toContain("PRIVATE_CANARY");
    expect(body.get("file")).toBeInstanceOf(Blob);
  });
  test("native: image edits accept only typed allowlisted FormData fields", async () => {
    responseType = "application/json"; responseData = Buffer.from('{"data":[]}');
    const body = new FormData(); body.set("image", new Blob([png], { type: "image/png" })); body.set("untrusted_file", new Blob([png], { type: "image/png" }));
    install(); await inNative(async () => { await (await fetch("https://api.openai.com/v1/images/edits", { method: "POST", body })).json(); });
    await settle(); expect(uploads).toEqual([png]);
  });
  test("wire-only transcription FormData is held (unread) until the wire span exists, then uploads once bound to it", async () => {
    responseType = "application/json"; responseData = Buffer.from('{"text":"synthetic"}');
    install(); const pending = transcription();
    // Admitted before the span exists: bytes are reserved, nothing is sent until bind.
    expect(manager.status().ownedBytes).toBe(audio.length); expect(sessionsOnly()).toEqual([]);
    expect(await (await pending).json()).toEqual({ text: "synthetic" });
    await settle(); expect(uploads).toEqual([audio]); expect(providerBodies[0].includes(audio)).toBe(true);
    const spans = exporter.getFinishedSpans(); expect(spans).toHaveLength(1);
    expect(requests[0]).toMatchObject({ direction: "input", purpose: "model_input", source_kind: "Blob", mime_type: "audio/wav" });
    expect(requests[0].association.parent.span_id).toBe(spans[0].spanContext().spanId);
    expect(JSON.stringify(requests)).not.toContain("PRIVATE_CANARY");
    expect(manager.status()).toMatchObject({ openTickets: 0, ownedBytes: 0 });
  });
  test("wire-only image edits have no wire span: the held file is dropped when the call completes and nothing uploads", async () => {
    responseType = "application/json"; responseData = Buffer.from('{"data":[]}');
    const body = new FormData(); body.set("image", new Blob([png], { type: "image/png" }));
    install(); await (await fetch("https://api.openai.com/v1/images/edits", { method: "POST", body })).json();
    expect(manager.status()).toMatchObject({ openTickets: 0, ownedBytes: 0 });
    await settle(); expect(uploads).toHaveLength(0); expect(sessionsOnly()).toEqual([]);
  });
  test("a wire span that is not sampled uploads nothing held for it (re-checked at bind)", async () => {
    responseType = "application/json"; responseData = Buffer.from('{"text":"synthetic"}');
    await provider.shutdown(); exporter = new InMemorySpanExporter();
    provider = new BasicTracerProvider({ sampler: new AlwaysOffSampler(), spanProcessors: [new SimpleSpanProcessor(exporter)] });
    install(); await (await transcription()).json();
    responseType = "audio/wav"; responseData = audio; responseChunks = 4; expect(await readAll(await speech())).toEqual(audio);
    await settle(); expect(uploads).toHaveLength(0); expect(sessionsOnly()).toEqual([]);
    expect(manager.status()).toMatchObject({ openStreams: 0, openTickets: 0, ownedBytes: 0 });
  });
  test("wire-only image JSON output uploads decoded bytes through the provider adapter, never URLs", async () => {
    responseType = "application/json"; responseData = Buffer.from(JSON.stringify({ data: [{ b64_json: png.toString("base64") }, { url: "https://untrusted.invalid/private?token=CANARY" }] }));
    install(); const body = await (await fetch("https://api.openai.com/v1/images/generations", { method: "POST", body: JSON.stringify({ model: "gpt-image-1", prompt: "synthetic" }) })).json();
    expect(body.data[0].b64_json).toBe(png.toString("base64")); await settle(); expect(uploads).toEqual([png]);
    expect(providerRequests).toBe(1);
    expect(requests[0]).toMatchObject({ direction: "output", purpose: "model_output", mime_type: "image/png", message_index: 0, part_index: 0 });
    expect(JSON.stringify(requests)).not.toContain("CANARY"); expect(JSON.stringify(exporter.getFinishedSpans().map(s => s.attributes))).not.toContain(png.toString("base64"));
  });
  test("wire-only chat and responses JSON input: images and computer screenshots via the provider adapter", async () => {
    responseType = "application/json"; responseData = Buffer.from('{"id":"synthetic","object":"chat.completion","choices":[]}');
    const uri = `data:image/png;base64,${png.toString("base64")}`;
    install();
    await (await fetch("https://api.openai.com/v1/chat/completions", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ model: "gpt-4o", messages: [{ role: "user", content: [{ type: "text", text: "synthetic" }, { type: "image_url", image_url: { url: uri } }] }] }) })).json();
    responseData = Buffer.from('{"id":"synthetic","object":"response","output":[]}');
    await (await fetch("https://api.openai.com/v1/responses", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ model: "computer-use-preview", input: [{ type: "computer_call_output", call_id: "call_synthetic", output: { type: "computer_screenshot", image_url: uri } }] }) })).json();
    await settle(); expect(uploads).toEqual([png, png]);
    expect(requests.map(r => r.purpose)).toEqual(["model_input", "computer_screenshot"]);
    expect(requests[1].tool_call_id).toBe("call_synthetic");
    const spans = exporter.getFinishedSpans();
    expect(requests.map(r => r.association.parent.span_id)).toEqual(spans.map(s => s.spanContext().spanId));
  });
  test("a provider adapter capture on the same span and position is not uploaded again by wire (#682 registry)", async () => {
    responseType = "application/json"; responseData = Buffer.from('{"id":"synthetic","object":"chat.completion","choices":[]}');
    const payload = { model: "gpt-4o", messages: [{ role: "user", content: [{ type: "image_url", image_url: { url: `data:image/png;base64,${png.toString("base64")}` } }] }] };
    install(); await inNative(async () => {
      captureOpenAIAttachments(manager, trace.getSpan(context.active())!, payload, { operation: "chat", direction: "input" });
      await (await fetch("https://api.openai.com/v1/chat/completions", { method: "POST", body: JSON.stringify(payload) })).json();
    });
    await settle(); expect(uploads).toEqual([png]);
  });
});

describe("privacy negative controls", () => {
  test.each([
    ["disabled", { enabled: false }], ["hide inputs", { traceConfig: { hideInputs: true } }], ["hide text", { traceConfig: { hideInputText: true } }],
    ["hide images", { traceConfig: { hideInputImages: true } }], ["PII", { traceConfig: { piiRedaction: true } }],
  ] as [string, AttachmentManagerOptions][])("%s prevents input image transfer at the network boundary", async (_name, options) => {
    await manager.shutdown(); manager = await createManager(options); transfers = [];
    responseType = "application/json"; responseData = Buffer.from('{"data":[]}');
    const body = new FormData(); body.set("image", new Blob([png], { type: "image/png" }), "PRIVATE_CANARY.png");
    install(); await inNative(async () => { await (await fetch("https://api.openai.com/v1/images/edits", { method: "POST", body })).json(); });
    await (await fetch("https://api.openai.com/v1/chat/completions", { method: "POST",
      body: JSON.stringify({ messages: [{ role: "user", content: [{ type: "image_url", image_url: { url: `data:image/png;base64,${png.toString("base64")}` } }] }] }) })).json();
    // Wire-only FormData audio through the deferred target: denied at admission, nothing held (image hiding is image-only).
    if (!options.traceConfig?.hideInputImages) { responseData = Buffer.from('{"text":"synthetic"}'); await (await transcription()).json(); }
    await settle(); expect(sessionsOnly()).toEqual([]); expect(manager.status()).toMatchObject({ openTickets: 0, ownedBytes: 0 });
  });
  test.each(["hideInputs", "hideInputMessages", "piiRedaction"] as const)("%s set after a wire-only file was held denies it at bind", async flag => {
    responseType = "application/json"; responseData = Buffer.from('{"text":"synthetic"}');
    install(); const pending = transcription();
    expect(manager.status().ownedBytes).toBe(audio.length);
    manager.updatePolicy({ traceConfig: { [flag]: true } }); transfers = [];
    await (await pending).json();
    await settle(); expect(sessionsOnly()).toEqual([]); expect(uploads).toHaveLength(0); expect(manager.status()).toMatchObject({ openTickets: 0, ownedBytes: 0 });
  });
  test.each(["hideOutputs", "hideOutputMessages", "hideOutputText", "piiRedaction"] as const)("%s prevents output speech and image transfer", async flag => {
    manager.updatePolicy({ traceConfig: { [flag]: true } }); transfers = [];
    install(); await inNative(async () => { await (await speech()).arrayBuffer(); });
    await (await speech()).arrayBuffer();
    responseChunks = 4; await readAll(await speech()); responseChunks = 1;
    responseType = "application/json"; responseData = Buffer.from(JSON.stringify({ data: [{ b64_json: png.toString("base64") }] }));
    await (await fetch("https://api.openai.com/v1/images/generations", { method: "POST", body: "{}" })).json();
    await settle(); expect(sessionsOnly()).toEqual([]);
  });
  test("T-PF-09 server-required redaction denies before any base64 decode", async () => {
    await manager.shutdown(); manager = await createManager({}, { require_media_redaction: true }); transfers = [];
    responseType = "application/json"; responseData = Buffer.from(JSON.stringify({ data: [{ b64_json: png.toString("base64") }] }));
    install(); const decode = jest.spyOn(Buffer, "from");
    await (await fetch("https://api.openai.com/v1/images/generations", { method: "POST", body: "{}" })).json();
    await inNative(async () => { await (await fetch("https://api.openai.com/v1/images/generations", { method: "POST", body: "{}" })).json(); });
    const calls = decode.mock.calls.filter(args => args[0] === png.toString("base64") && (args as unknown[])[1] === "base64"); decode.mockRestore();
    await settle(); expect(calls).toHaveLength(0); expect(sessionsOnly()).toEqual([]);
  });
  test("unknown host, non-audio MIME and error bodies do not upload", async () => {
    install(); await inNative(async () => {
      await (await speech("unknown.invalid/v1/audio/speech")).arrayBuffer();
      // A lookalike host or port under a native span: only the media allowlist stands between it and capture.
      await (await speech("api.openai.com.evil.invalid/v1/audio/speech")).arrayBuffer();
      await (await speech("api.openai.com:8443/v1/audio/speech")).arrayBuffer();
      responseType = "application/octet-stream"; await (await speech()).arrayBuffer();
      responseType = "audio/wav"; responseStatus = 403; await (await speech()).arrayBuffer();
    });
    responseStatus = 200; await (await speech("api.openai.com.evil.invalid/v1/audio/speech")).arrayBuffer();
    await settle(); expect(uploads).toHaveLength(0);
  });
  test("oversized media, raw multipart, and ended/unrelated native contexts fail closed", async () => {
    responseType = "application/json"; responseData = Buffer.from('{"data":[]}');
    const body = new FormData(); body.set("image", new Blob([new Uint8Array(20 * 1024 * 1024 + 1)], { type: "image/png" }));
    install(); await inNative(async () => {
      await (await fetch("https://api.openai.com/v1/images/edits", { method: "POST", body })).json();
      await (await fetch("https://api.openai.com/v1/images/edits", { method: "POST", headers: { "content-type": "multipart/form-data; boundary=x" }, body: "--x\r\nPRIVATE_CANARY" })).json();
    });
    const small = new FormData(); small.set("image", new Blob([png], { type: "image/png" }));
    await inNative(async () => { await (await fetch("https://api.openai.com/v1/images/edits", { method: "POST", body: small })).json(); }, "TOOL");
    await settle(); expect(uploads).toHaveLength(0); expect(manager.status()).toMatchObject({ openTickets: 0, ownedBytes: 0 });
  });
  test("policy tightening between request and application consumption prevents capture", async () => {
    install(); await inNative(async () => { const response = await speech(); manager.updatePolicy({ traceConfig: { hideOutputs: true } }); await response.arrayBuffer(); });
    await settle(); expect(uploads).toHaveLength(0);
  });
  test("hook removal makes retained response wrappers inert", async () => {
    install(); await inNative(async () => { const response = await speech(); hook!.remove(); await response.arrayBuffer(); });
    await settle(); expect(uploads).toHaveLength(0);
  });
  test("native capture is denied without an explicit integration policy", async () => {
    install(); await inNative(async () => { await (await speech()).arrayBuffer(); }, "LLM", false);
    await settle(); expect(uploads).toHaveLength(0);
  });
  test.each([{ captureContent: false }, { recordOutputs: false }, { muted: true }, { temporaryChat: true }])("integration restrictions cannot be loosened by child scopes: %j", async restriction => {
    install(); await inNative(() => context.with(withAttachmentCapturePolicy(withAttachmentCapturePolicy(context.active(), restriction), { captureContent: true, recordOutputs: true, muted: false, temporaryChat: false }), async () => {
      await (await speech()).arrayBuffer();
    })); await settle(); expect(uploads).toHaveLength(0);
  });
  test("a disabled manager installs no media observer: unchanged provider traffic, no control traffic", async () => {
    const disabled = new AttachmentManager({ enabled: false, environment: () => ({}) });
    transfers = []; install(disabled); await inNative(async () => { await (await speech()).arrayBuffer(); });
    await (await speech()).arrayBuffer();
    await settle(); expect(transfers).toEqual([]); expect(providerRequests).toBe(2);
  });
});

describe("A02 ownership and configuration", () => {
  test("borrowed manager identity survives auto setup; auto adds only restrictions", async () => {
    const config = resolveConfig({ traceConfig: { hideOutputs: true }, projectName: "auto-project", configFile: "/nonexistent" }, {});
    const selected = selectAttachmentManager({ attachments: manager }, config, {}, false);
    expect(selected.manager).toBe(manager); expect(selected.owned).toBe(false);
    expect(attachmentOptions(config, { attachments: { projectName: "wrong-project", traceConfig: { hideOutputs: false } } })).toMatchObject({ projectName: "auto-project", traceConfig: { hideOutputs: true } });
    install(); await inNative(async () => { await (await speech()).arrayBuffer(); });
    await settle(); expect(uploads).toHaveLength(0);
  });
  test("FI_ATTACHMENTS_ENABLED=false denies at admission on a process-env manager, without permanently disabling a borrowed one", async () => {
    const before = process.env.FI_ATTACHMENTS_ENABLED;
    delete process.env.FI_ATTACHMENTS_ENABLED;
    try {
      // A manager whose `environment` reads process.env, as auto's own and the Node factory's default do.
      await manager.shutdown(); manager = await createManager({ environment: () => process.env });
      process.env.FI_ATTACHMENTS_ENABLED = "false";
      selectAttachmentManager({ attachments: manager }, resolveConfig({ configFile: "/nonexistent" }, {}), {}, false);
      transfers = []; install();
      // Kill switch at admission: materialized, incremental and FormData wire captures, native and wire-only, all denied.
      await inNative(async () => { await (await speech()).arrayBuffer(); });
      await (await speech()).arrayBuffer();
      responseType = "application/json"; responseData = Buffer.from('{"text":"synthetic"}'); await (await transcription()).json();
      await settle(); expect(sessionsOnly()).toEqual([]); expect(uploads).toHaveLength(0);
      expect(manager.status()).toMatchObject({ openTickets: 0, openStreams: 0, ownedBytes: 0 });
      // The switch is read at each admission: lifting it resumes capture on the same borrowed manager.
      delete process.env.FI_ATTACHMENTS_ENABLED;
      responseType = "audio/wav"; responseData = audio;
      await inNative(async () => { await (await speech()).arrayBuffer(); });
      await settle(); expect(uploads).toEqual([audio]);
    } finally { if (before === undefined) delete process.env.FI_ATTACHMENTS_ENABLED; else process.env.FI_ATTACHMENTS_ENABLED = before; }
  });
  test("status report is policy and source aware and claims no playback", () => {
    const report = attachmentReport(manager);
    expect(report.state).toBe("ready"); expect(report.status).toMatchObject({ openTickets: 0 });
    expect(report.routes).toContain("POST https://api.openai.com/v1/audio/speech (speech)");
    expect(report.unsupported).toEqual(expect.arrayContaining(["WebSocket / RTC audio", "product playback qualification"]));
    expect(report.unsupported.join("\n")).not.toMatch(/deferred-target preflight/);
    expect(report.sources).toEqual(expect.arrayContaining([expect.stringContaining("wire-only FormData Blob"), expect.stringContaining("wire-only speech audio read incrementally")]));
    expect(attachmentReport().state).toBe("not_initialized");
  });
});

describe("real SDKs against loopback", () => {
  const sdk = () => require(process.env.ATT_OPENAI_MODULE || "openai");
  // The package's exports map hides package.json; walk up from the resolved entry instead.
  const sdkVersion = (): string => {
    let dir = dirname(require.resolve(process.env.ATT_OPENAI_MODULE || "openai"));
    for (let i = 0; i < 8; i++, dir = dirname(dir)) {
      try { const pkg = JSON.parse(readFileSync(join(dir, "package.json"), "utf8")); if (pkg.name === "openai") return pkg.version; } catch { /* keep walking */ }
    }
    return "0.0.0";
  };
  test("OpenAI SDK speech (wire-only) preserves WAV bytes and correlation", async () => {
    const OpenAI = sdk().default ?? sdk().OpenAI;
    install(); const client = new OpenAI({ apiKey: "synthetic-provider-key", fetch: (...args: Parameters<typeof fetch>) => globalThis.fetch(...args) });
    const response = await client.audio.speech.create({ model: "tts-1", voice: "alloy", input: "synthetic", response_format: "wav" });
    expect(Buffer.from(await response.arrayBuffer())).toEqual(audio);
    await settle(); expect(uploads).toEqual([audio]); expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(requests[0].association.parent.span_id).toBe(exporter.getFinishedSpans()[0].spanContext().spanId);
  });
  test("OpenAI SDK image generation (wire-only) uploads the decoded PNG", async () => {
    const OpenAI = sdk().default ?? sdk().OpenAI;
    responseType = "application/json"; responseData = Buffer.from(JSON.stringify({ created: 1, data: [{ b64_json: png.toString("base64") }] }));
    install(); const client = new OpenAI({ apiKey: "synthetic-provider-key", fetch: (...args: Parameters<typeof fetch>) => globalThis.fetch(...args) });
    const result = await client.images.generate({ model: "gpt-image-1", prompt: "synthetic" });
    expect(result.data[0].b64_json).toBe(png.toString("base64"));
    await settle(); expect(uploads).toEqual([png]);
  });
  test("OpenAI SDK transcription (wire-only) keeps the multipart request intact and uploads the file once, bound to the wire span", async () => {
    const mod = sdk();
    // openai 4.x sends multipart as a Node stream, which a WHATWG fetch rejects without `duplex` (SDK limitation, not auto).
    if (Number(sdkVersion().split(".")[0]) < 5) return; const OpenAI = mod.default ?? mod.OpenAI; const toFile = mod.toFile;
    responseType = "application/json"; responseData = Buffer.from('{"text":"synthetic"}');
    install(); const client = new OpenAI({ apiKey: "synthetic-provider-key", fetch: (...args: Parameters<typeof fetch>) => globalThis.fetch(...args) });
    const result = await client.audio.transcriptions.create({ model: "whisper-1", file: await toFile(audio, "synthetic.wav", { type: "audio/wav" }) });
    expect(result.text).toBe("synthetic"); expect(providerBodies[0].includes(audio)).toBe(true);
    await settle(); expect(uploads).toEqual([audio]);
    const spans = exporter.getFinishedSpans(); expect(spans).toHaveLength(1);
    expect(requests[0]).toMatchObject({ direction: "input", purpose: "model_input", source_kind: "Blob" });
    expect(requests[0].association.parent.span_id).toBe(spans[0].spanContext().spanId);
  });
});
