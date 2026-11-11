import { createServer, type Server } from "node:http";
import { AddressInfo } from "node:net";
import { context, trace, ROOT_CONTEXT } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { AttachmentManager, type AttachmentManagerOptions, type AttachmentTransport } from "@traceai/fi-core/attachments";
import { patchFetch } from "../src/wire/fetch";
import { withAttachmentOwnership, withAttachmentCapturePolicy } from "../src/wire/attachments";
import { attachmentOptions, selectAttachmentManager, attachmentReport } from "../src/attachments";
import { resolveConfig } from "../src/config";

// Synthetic media/credentials only. Both provider and attachment traffic terminate on loopback.
const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64");
function wav() {
  const b = Buffer.alloc(44 + 160); b.write("RIFF"); b.writeUInt32LE(b.length - 8, 4); b.write("WAVEfmt ", 8);
  b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22); b.writeUInt32LE(8000, 24);
  b.writeUInt32LE(16000, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write("data", 36); b.writeUInt32LE(160, 40); return b;
}
const audio = wav();
const uuid = "11111111-1111-4111-8111-111111111111";
let original: typeof fetch, server: Server, origin: string, manager: AttachmentManager, hook: ReturnType<typeof patchFetch> | undefined;
let uploads: Buffer[], requests: any[], transfers: { path: string; body: Buffer }[], providerBodies: Buffer[];
let provider: BasicTracerProvider, exporter: InMemorySpanExporter;
let responseStatus: number, responseData: Buffer, responseType: string;
const nativeFetch = globalThis.fetch;
const sessionPolicy = { version: "1", enabled: true, allowed_sources: ["Bytes", "Blob"] as ("Bytes" | "Blob")[], allowed_modalities: ["image", "audio"] as ("image" | "audio")[], require_media_redaction: false, require_metadata_removal: false, allow_unredacted_media_with_text_pii: false, record_audio: false, record_video: false };
async function createManager(options: AttachmentManagerOptions = {}, policy = sessionPolicy) {
  const send = async (path: string, body: BodyInit) => {
    const response = await nativeFetch(`${origin}${path}`, { method: "POST", body });
    return response.json();
  };
  const transport: AttachmentTransport = {
    async bootstrap(request) { await send("/attachment-sessions", JSON.stringify(request)); return { schema_version: 1, scope: { organization_id: uuid, workspace_id: uuid, project_id: uuid }, capture_session_id: request.capture_session_id, policy, limits: { max_upload_bytes: 20971520, segment_bytes: 8388608, segment_duration_ms: 30000, recording_bytes: 1073741824, recording_duration_ms: 7200000, active_transfers: 16, active_recordings: 4, reserved_bytes: 1073741824, validation_jobs: 1024, initializations_per_minute: 120, links_per_minute: 1000, upload_bytes_per_second: 1073741824, playback_bytes_per_second: 1073741824, transfer_timeout_seconds: 600, grant_ttl_seconds: 300, revocation_check_seconds: 5 }, expires_at: new Date(Date.now() + 60000).toISOString(), transfer_modes: ["authenticated_stream"] }; },
    async initialize(_session, request) { await send("/uploads", JSON.stringify(request)); requests.push(request); return { upload_id: uuid, attachment_id: request.attachment_id, occurrence_id: request.occurrence_id, state: "pending_upload", reserved_bytes: request.declared_bytes, expires_at: new Date(Date.now() + 60000).toISOString(), transfer_mode: "authenticated_stream", write_capability: "synthetic-capability" }; },
    async upload(_session, _upload, bytes) { await send("/bytes", bytes as BodyInit); },
    async complete() { await send("/complete", "{}"); },
  };
  const result = new AttachmentManager({ enabled: true, projectName: "attachment-test", projectType: "observe", environment: () => ({}), transport, ...options });
  expect(await result.ready()).toBe(options.enabled !== false);
  expect(result.status().state).toBe(options.enabled === false ? "disabled" : "queued");
  return result;
}
function install() {
  globalThis.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const u = new URL(input instanceof Request ? input.url : String(input));
    return nativeFetch(`${origin}${u.pathname}${u.search}`, init);
  };
  hook = patchFetch(4194304, (result, parent, start, end, bindMedia) => {
    const span = provider.getTracer("wire-test").startSpan(result.name, { startTime: start, attributes: result.attributes }, parent);
    bindMedia?.(span); span.end(end);
  }, [], manager);
}
function inNative<T>(fn: () => T, kind = "LLM"): T {
  const span = provider.getTracer("native-test").startSpan("native", { attributes: { "gen_ai.span.kind": kind } });
  const result = context.with(withAttachmentCapturePolicy(trace.setSpan(ROOT_CONTEXT, span), { captureContent: true }), fn);
  // Promise work retains this exact request context; do not consult later ambient spans.
  if (result instanceof Promise) return result.finally(() => span.end()) as T;
  span.end(); return result;
}
async function settle() { await manager.flush(2000); }
beforeAll(() => context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable()));
afterAll(() => context.disable());
beforeEach(async () => {
  original = globalThis.fetch; uploads = []; requests = []; transfers = []; providerBodies = [];
  responseStatus = 200; responseData = audio; responseType = "audio/wav";
  server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => {
      const body = Buffer.concat(chunks);
      if (["/attachment-sessions", "/uploads", "/bytes", "/complete"].includes(req.url!)) {
        transfers.push({ path: req.url!, body }); if (req.url === "/bytes") uploads.push(body);
        res.writeHead(200, { "content-type": "application/json" }); res.end("{}");
      } else { providerBodies.push(body); res.writeHead(responseStatus, { "content-type": responseType }); res.end(responseData); }
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

test.each(["arrayBuffer", "blob"] as const)("caller-consumed speech %s uploads once with the actual wire identity", async method => {
  install(); const response = await fetch("https://api.openai.com/v1/audio/speech", { method: "POST", body: JSON.stringify({ model: "tts-1", input: "synthetic", voice: "alloy" }) });
  const result = await response[method]();
  expect(Buffer.from(result instanceof Blob ? await result.arrayBuffer() : result)).toEqual(audio);
  await settle(); expect(uploads).toEqual([audio]);
  const span = exporter.getFinishedSpans()[0]; expect(requests[0].association.parent.span_id).toBe(span.spanContext().spanId);
  expect(span.attributes["fi.attachment.correlation_nonce"]).toBeTruthy();
  // Completion never mutates an ended span; authoritative late links belong to the API.
  expect(span.attributes["fi.attachment.refs"]).toBe("[]");
});
test("native semantic coverage leaves safe media observation on without a second span", async () => {
  install(); await inNative(async () => { const response = await fetch("https://api.openai.com/v1/audio/speech", { method: "POST" }); await response.arrayBuffer(); });
  await settle(); expect(uploads).toEqual([audio]); expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(requests[0].association.parent.span_id).toBe(exporter.getFinishedSpans()[0].spanContext().spanId);
});
test("adapter media ownership is independent and prevents duplicate uploads", async () => {
  install(); await inNative(() => context.with(withAttachmentOwnership(context.active()), async () => { await (await fetch("https://api.openai.com/v1/audio/speech", { method: "POST" })).arrayBuffer(); }));
  await settle(); expect(uploads).toHaveLength(0); expect(exporter.getFinishedSpans()).toHaveLength(1);
});
test.each(["api.openai.com/v1/audio/speech", "api.deepgram.com/v1/speak"])("recognized %s preserves cloned application reads and deduplicates output", async route => {
  install(); await inNative(async () => {
    const response = await fetch(`https://${route}`, { method: "POST" }); const clone = response.clone();
    const [one, two] = await Promise.all([response.arrayBuffer(), clone.blob()]); expect(Buffer.from(one)).toEqual(audio); expect(Buffer.from(await two.arrayBuffer())).toEqual(audio);
  }); await settle(); expect(uploads).toEqual([audio]);
});
test("unread, cancelled and reader-consumed responses do not create eager media readers", async () => {
  install(); await inNative(async () => {
    const unread = await fetch("https://api.openai.com/v1/audio/speech", { method: "POST" });
    expect(unread.bodyUsed).toBe(false); await unread.body!.cancel();
    const consumed = await fetch("https://api.openai.com/v1/audio/speech", { method: "POST" });
    const reader = consumed.body!.getReader(); const chunks = []; for (;;) { const x = await reader.read(); if (x.done) break; chunks.push(Buffer.from(x.value)); }
    expect(Buffer.concat(chunks)).toEqual(audio);
  }); await settle(); expect(uploads).toHaveLength(0);
});
test("FormData keeps the body, boundary and filename on the provider path only", async () => {
  responseType = "application/json"; responseData = Buffer.from('{"text":"synthetic"}');
  const body = new FormData(); body.set("model", "whisper-1"); body.set("file", new Blob([audio], { type: "audio/wav" }), "PRIVATE_CANARY_FILENAME.wav");
  install(); await inNative(async () => { await (await fetch("https://api.openai.com/v1/audio/transcriptions", { method: "POST", body })).json(); });
  await settle(); expect(uploads).toEqual([audio]); expect(providerBodies[0].includes(audio)).toBe(true); expect(providerBodies[0].toString()).toContain("PRIVATE_CANARY_FILENAME.wav");
  expect(JSON.stringify(requests)).not.toContain("PRIVATE_CANARY"); expect(JSON.stringify(exporter.getFinishedSpans().map(s => s.attributes))).not.toContain("PRIVATE_CANARY");
  expect(body.get("file")).toBeInstanceOf(Blob);
});
test("image edits accept only typed allowlisted FormData fields", async () => {
  responseType = "application/json"; responseData = Buffer.from('{"data":[]}');
  const body = new FormData(); body.set("image", new Blob([png], { type: "image/png" })); body.set("untrusted_file", new Blob([png], { type: "image/png" }));
  install(); await inNative(async () => { await (await fetch("https://api.openai.com/v1/images/edits", { method: "POST", body })).json(); });
  await settle(); expect(uploads).toEqual([png]);
});
test("wire-only FormData remains unsupported without retaining the source until a late span", async () => {
  responseType = "application/json"; responseData = Buffer.from('{"text":"synthetic"}');
  const body = new FormData(); body.set("model", "whisper-1"); body.set("file", new Blob([audio], { type: "audio/wav" }));
  install(); await (await fetch("https://api.openai.com/v1/audio/transcriptions", { method: "POST", body })).json();
  await settle(); expect(uploads).toHaveLength(0);
});
test("application-decoded PNG image JSON uploads bytes, not URLs or base64 span fields", async () => {
  responseType = "application/json"; responseData = Buffer.from(JSON.stringify({ data: [{ b64_json: png.toString("base64") }, { url: "https://untrusted.invalid/private?token=CANARY" }] }));
  install(); const body = await (await fetch("https://api.openai.com/v1/images/generations", { method: "POST", body: JSON.stringify({ model: "dall-e-3", prompt: "synthetic" }) })).json();
  expect(body.data[0].b64_json).toBe(png.toString("base64")); await settle(); expect(uploads).toEqual([png]);
  expect(JSON.stringify(requests)).not.toContain("CANARY"); expect(JSON.stringify(exporter.getFinishedSpans().map(s => s.attributes))).not.toContain(png.toString("base64"));
});
test.each([
  ["disabled", { enabled: false }], ["hide inputs", { traceConfig: { hideInputs: true } }], ["hide text", { traceConfig: { hideInputText: true } }],
  ["hide images", { traceConfig: { hideInputImages: true } }], ["PII", { traceConfig: { piiRedaction: true } }],
] as [string, AttachmentManagerOptions][])("%s prevents image transfer at the network boundary", async (_name, options) => {
  await manager.shutdown(); manager = await createManager(options); transfers = [];
  responseType = "application/json"; responseData = Buffer.from('{"data":[]}');
  const body = new FormData(); body.set("image", new Blob([png], { type: "image/png" }), "PRIVATE_CANARY.png");
  install(); await inNative(async () => { await (await fetch("https://api.openai.com/v1/images/edits", { method: "POST", body })).json(); });
  await settle(); expect(transfers).toEqual([]);
});
test.each(["hideOutputs", "hideOutputMessages", "hideOutputText", "piiRedaction"] as const)("%s prevents output speech transfer", async flag => {
  manager.updatePolicy({ traceConfig: { [flag]: true } }); transfers = [];
  install(); await inNative(async () => { await (await fetch("https://api.openai.com/v1/audio/speech", { method: "POST" })).arrayBuffer(); });
  await settle(); expect(transfers).toEqual([]);
});
test("server-required redaction rejects decoded PNG before the base64 decoder", async () => {
  await manager.shutdown(); manager = await createManager({}, { ...sessionPolicy, require_media_redaction: true }); transfers = [];
  responseType = "application/json"; responseData = Buffer.from(JSON.stringify({ data: [{ b64_json: png.toString("base64") }] }));
  install(); const decode = jest.spyOn(Buffer, "from");
  await inNative(async () => { await (await fetch("https://api.openai.com/v1/images/generations", { method: "POST" })).json(); });
  const calls = decode.mock.calls.filter(args => args[0] === png.toString("base64") && (args as unknown[])[1] === "base64"); decode.mockRestore();
  await settle(); expect(calls).toHaveLength(0); expect(transfers).toEqual([]);
});
test("unknown provider, non-audio MIME and error bodies do not upload", async () => {
  install(); await inNative(async () => {
    await (await fetch("https://unknown.invalid/v1/audio/speech", { method: "POST" })).arrayBuffer();
    responseType = "application/octet-stream"; await (await fetch("https://api.openai.com/v1/audio/speech", { method: "POST" })).arrayBuffer();
    responseType = "audio/wav"; responseStatus = 403; await (await fetch("https://api.openai.com/v1/audio/speech", { method: "POST" })).arrayBuffer();
  }); await settle(); expect(uploads).toHaveLength(0);
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
  await settle(); expect(uploads).toHaveLength(0);
});
test("policy tightening between request and application consumption prevents capture", async () => {
  install(); await inNative(async () => {
    const response = await fetch("https://api.openai.com/v1/audio/speech", { method: "POST" }); manager.updatePolicy({ enabled: false }); await response.arrayBuffer();
  }); await settle(); expect(uploads).toHaveLength(0);
});
test("removal makes retained response wrappers inert", async () => {
  install(); await inNative(async () => { const response = await fetch("https://api.openai.com/v1/audio/speech", { method: "POST" }); hook!.remove(); await response.arrayBuffer(); });
  await settle(); expect(uploads).toHaveLength(0);
});
test("borrowed manager identity and restrictive policy survive auto setup", async () => {
  const config = resolveConfig({ traceConfig: { hideOutputs: true }, projectName: "auto-project", configFile: "/nonexistent" }, {});
  const selected = selectAttachmentManager({ attachments: manager }, config, {}, false);
  expect(selected.manager).toBe(manager); expect(selected.owned).toBe(false);
  expect(attachmentOptions(config, { attachments: { projectName: "wrong-project", traceConfig: { hideOutputs: false } } })).toMatchObject({ projectName: "auto-project", traceConfig: { hideOutputs: true } });
  expect(attachmentReport(manager).integration).toBe("component_only");
  install(); await inNative(async () => { await (await fetch("https://api.openai.com/v1/audio/speech", { method: "POST" })).arrayBuffer(); });
  await settle(); expect(uploads).toHaveLength(0);
});
test("real installed OpenAI SDK speech operation preserves WAV bytes and correlation", async () => {
  const OpenAI = require(process.env.ATT_OPENAI_MODULE || "openai").default;
  install(); const sdk = new OpenAI({ apiKey: "synthetic-provider-key", fetch: globalThis.fetch });
  const response = await sdk.audio.speech.create({ model: "tts-1", voice: "alloy", input: "synthetic", response_format: "wav" });
  expect(Buffer.from(await response.arrayBuffer())).toEqual(audio);
  await settle(); expect(uploads).toEqual([audio]); expect(exporter.getFinishedSpans()).toHaveLength(1);
});

test("native capture is denied without an explicit integration policy", async () => {
  install(); const span = provider.getTracer("external").startSpan("external", { attributes: { "gen_ai.span.kind": "LLM" } });
  await context.with(trace.setSpan(ROOT_CONTEXT, span), async () => { await (await fetch("https://api.openai.com/v1/audio/speech", { method: "POST" })).arrayBuffer(); });
  span.end(); await settle(); expect(uploads).toHaveLength(0);
});
test.each([{ captureContent: false }, { recordOutputs: false }, { muted: true }, { temporaryChat: true }])("integration restrictions cannot be loosened by child scopes: %j", async restriction => {
  install(); await inNative(() => context.with(withAttachmentCapturePolicy(withAttachmentCapturePolicy(context.active(), restriction), { captureContent: true, recordOutputs: true, muted: false, temporaryChat: false }), async () => {
    await (await fetch("https://api.openai.com/v1/audio/speech", { method: "POST" })).arrayBuffer();
  })); await settle(); expect(uploads).toHaveLength(0);
});
