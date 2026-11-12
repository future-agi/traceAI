/*
 * Owner mark vs the wire layer, for one call seen by two layers (an adapter span around a provider SDK call, and auto's
 * wire capture of the same HTTP call). The adapter that captures claims the call with the shared context key
 * Symbol.for("traceai.auto.attachment_owner.v1"); the wire layer must then upload nothing for that call, including
 * wire-only incremental speech reads that would otherwise capture through a deferred target.
 *
 * The provider-media claim (@traceai/provider-media) is stubbed here through the public Symbol key, so this file
 * needs no adapter package: the stub claims exactly as an adapter does (around the SDK call only; the application
 * reads the body later, outside that context) and uploads its own copy, bound to its own span.
 * Provider and attachment traffic both terminate on loopback. Synthetic data only.
 */
import { createServer, request as httpRequest, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { AddressInfo } from "node:net";
import { randomUUID } from "node:crypto";
import { context, trace, ROOT_CONTEXT, type Span } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { AttachmentManager, createAttachmentHttpTransport, type AttachmentHttpClient } from "@traceai/fi-core/attachments";
import { patchFetch } from "../src/wire/fetch";
import { withAttachmentCapturePolicy } from "../src/wire/attachments";

const OWNER = Symbol.for("traceai.auto.attachment_owner.v1");
function wav(samples: number, step: number) {
  const b = Buffer.alloc(44 + samples * 2); b.write("RIFF"); b.writeUInt32LE(b.length - 8, 4); b.write("WAVEfmt ", 8);
  b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22); b.writeUInt32LE(8000, 24);
  b.writeUInt32LE(16000, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write("data", 36); b.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i++) b.writeInt16LE(Math.round(8000 * Math.sin(i / step)), 44 + i * 2);
  return b;
}
// Distinguishable payloads per provider route.
const openaiAudio = wav(80, 3), deepgramAudio = wav(96, 5);
const OPENAI_SPEECH = "https://api.openai.com/v1/audio/speech", DEEPGRAM_SPEAK = "https://api.deepgram.com/v1/speak?model=aura-asteria-en";

let server: Server, origin: string, manager: AttachmentManager, hook: ReturnType<typeof patchFetch> | undefined;
let provider: BasicTracerProvider, exporter: InMemorySpanExporter;
let uploads: Array<{ data: Buffer; request: any }>, pending: Map<string, any>, wireSpans: string[];
const nativeFetch = globalThis.fetch;
const serverPolicy = { version: "fixture-v1", enabled: true, allowed_sources: ["Bytes", "Blob"], allowed_modalities: ["image", "audio", "document", "file"],
  require_media_redaction: false, require_metadata_removal: false, allow_unredacted_media_with_text_pii: false, record_audio: false, record_video: false };
const limits = { max_upload_bytes: 20971520, segment_bytes: 8388608, segment_duration_ms: 30000, recording_bytes: 1073741824, recording_duration_ms: 7200000,
  active_transfers: 16, active_recordings: 4, reserved_bytes: 1073741824, validation_jobs: 1024, initializations_per_minute: 120, links_per_minute: 1000,
  upload_bytes_per_second: 1073741824, playback_bytes_per_second: 1073741824, transfer_timeout_seconds: 600, grant_ttl_seconds: 300, revocation_check_seconds: 5 };
const scope = { organization_id: randomUUID(), workspace_id: randomUUID(), project_id: randomUUID() };
const expiry = () => new Date(Date.now() + 3600000).toISOString();
function control(req: IncomingMessage, body: Buffer, res: ServerResponse) {
  res.setHeader("content-type", "application/json");
  if (req.url!.endsWith("attachment-sessions")) {
    const parsed = JSON.parse(body.toString());
    res.end(JSON.stringify({ schema_version: 1, scope, capture_session_id: parsed.capture_session_id, policy: serverPolicy, limits, expires_at: expiry(), transfer_modes: ["authenticated_stream"] }));
  } else if (req.url!.endsWith("/uploads")) {
    const parsed = JSON.parse(body.toString()); pending.set(parsed.attachment_id, parsed); res.statusCode = 201;
    res.end(JSON.stringify({ upload_id: parsed.attachment_id, attachment_id: parsed.attachment_id, occurrence_id: parsed.occurrence_id, state: "pending_upload",
      reserved_bytes: parsed.declared_bytes, expires_at: expiry(), transfer_mode: "authenticated_stream", write_capability: "synthetic-upload-only" }));
  } else if (req.url!.endsWith("/content")) {
    const id = req.url!.split("/").at(-2)!; uploads.push({ data: body, request: pending.get(id) });
    res.end(JSON.stringify({ upload_id: id, state: "pending_upload", reason: "queued" }));
  } else { res.statusCode = 202; res.end(JSON.stringify({ upload_id: req.url!.split("/").at(-2), state: "validating", reason: "binding_pending" })); }
}
const client: AttachmentHttpClient = async req => {
  const body = req.body instanceof Blob ? Buffer.from(await req.body.arrayBuffer()) : req.body;
  return new Promise((resolve, reject) => {
    const outgoing = httpRequest(`${origin}${new URL(req.url).pathname}`, { method: req.method, headers: { ...req.headers, "x-attachment-control": "1" }, signal: req.signal }, response => {
      const chunks: Buffer[] = []; response.on("data", c => chunks.push(c)); response.on("end", () => resolve({ status: response.statusCode!, headers: {}, body: Buffer.concat(chunks) } as any));
    }); outgoing.on("error", reject); outgoing.end(body as any);
  });
};

beforeAll(() => context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable()));
afterAll(() => context.disable());
beforeEach(async () => {
  uploads = []; pending = new Map(); wireSpans = [];
  server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", c => chunks.push(c));
    req.on("end", () => {
      const body = Buffer.concat(chunks);
      if (req.headers["x-attachment-control"]) { control(req, body, res); return; }
      if (req.url!.startsWith("/v1/audio/transcriptions")) { res.writeHead(200, { "content-type": "application/json" }); res.end('{"text":"synthetic"}'); return; }
      const data = req.url!.startsWith("/v1/speak") ? deepgramAudio : openaiAudio;
      res.writeHead(200, { "content-type": "audio/wav" });
      // Several spaced network chunks, so incremental readers see several reads.
      void (async () => { for (let i = 0; i < data.length; i += 24) { res.write(data.subarray(i, i + 24)); await new Promise(r => setTimeout(r, 10)); } res.end(); })();
    });
  });
  await new Promise<void>(done => server.listen(0, "127.0.0.1", done)); origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  exporter = new InMemorySpanExporter(); provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  manager = new AttachmentManager({ enabled: true, projectName: "attachment-owner-test", projectType: "observe", environment: () => ({}),
    transport: createAttachmentHttpTransport({ endpoint: "https://attachments.example", headers: { authorization: "Bearer synthetic" }, client }) });
  expect(await manager.ready()).toBe(true);
  // Auto's real fetch hook over a loopback redirect; the emitter binds media to the actual wire span.
  globalThis.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const u = new URL(input instanceof Request ? input.url : String(input));
    return nativeFetch(`${origin}${u.pathname}${u.search}`, init);
  };
  hook = patchFetch(4194304, (result, parent, start, end, bindMedia) => {
    const span = provider.getTracer("wire").startSpan(result.name, { startTime: start, attributes: result.attributes }, parent);
    wireSpans.push(span.spanContext().spanId); bindMedia?.(span); span.end(end);
  }, [], manager);
});
afterEach(async () => {
  hook?.remove(); hook = undefined; globalThis.fetch = nativeFetch;
  await manager.shutdown(1000); await provider.shutdown();
  server.closeAllConnections(); await new Promise<void>(done => server.close(() => done()));
});

/**
 * A provider adapter (stand-in for @traceai/provider-media): its own span, the owner claim around the provider call
 * only, and its own upload of the bytes the application read. Returns what the call returned.
 */
function adapter() {
  const span: Span = provider.getTracer("adapter").startSpan("Adapter Audio speech", { attributes: { "fi.span.kind": "LLM" } });
  return {
    span,
    claim: <T>(fn: () => T): T => context.with(trace.setSpan(context.active(), span).setValue(OWNER, true), fn),
    capture(data: Buffer) {
      const ticket = manager.preflight(manager.correlation(span), { mimeType: "audio/wav", direction: "output", purpose: "model_output", maxBytes: data.length, sourceKind: "Bytes" });
      if (ticket.admitted) ticket.attach({ kind: "Bytes", data: new Uint8Array(data) });
      span.end();
    },
  };
}
const post = (url: string) => fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: "synthetic", input: "synthetic" }) });
async function read(response: Response, mode: "reader" | "iterator" | "arrayBuffer"): Promise<Buffer> {
  if (mode === "arrayBuffer") return Buffer.from(await response.arrayBuffer());
  const chunks: Buffer[] = [];
  if (mode === "iterator") { for await (const chunk of response.body as any) chunks.push(Buffer.from(chunk)); return Buffer.concat(chunks); }
  const reader = response.body!.getReader();
  for (;;) { const x = await reader.read(); if (x.done) return Buffer.concat(chunks); chunks.push(Buffer.from(x.value)); }
}
const parentOf = (data: Buffer) => uploads.find(u => Buffer.compare(u.data, data) === 0)?.request.association.parent.span_id;
/** "policy": an outer integration supplied an explicit capture policy, so without the claim the wire layer would also capture against the adapter's model span. */
const outerContext = (p: "policy" | "no-policy") => p === "policy" ? withAttachmentCapturePolicy(ROOT_CONTEXT, { captureContent: true }) : ROOT_CONTEXT;
const settle = () => manager.flush(2000);
const idle = () => expect(manager.status()).toMatchObject({ openTickets: 0, openStreams: 0, ownedBytes: 0 });

describe("one call, two layers: the owner mark keeps wire capture out", () => {
  test.each([[OPENAI_SPEECH, openaiAudio], [DEEPGRAM_SPEAK, deepgramAudio]] as const)("positive control: unowned wire-only %s read incrementally uploads once, bound to the wire span", async (url, data) => {
    expect(await read(await post(url), "reader")).toEqual(data);
    await settle(); expect(uploads.map(u => u.data)).toEqual([data]);
    expect(wireSpans).toContain(parentOf(data)); idle();
  });

  for (const [route, url, data] of [["OpenAI speech", OPENAI_SPEECH, openaiAudio], ["Deepgram speak", DEEPGRAM_SPEAK, deepgramAudio]] as const) {
    for (const mode of ["reader", "iterator", "arrayBuffer"] as const) for (const p of ["policy", "no-policy"] as const) {
      test(`adapter-owned ${route} (${mode}, ${p}, body read after the claim scope ended): exactly one upload, the adapter's`, async () => {
        await context.with(outerContext(p), async () => {
          const a = adapter();
          const response = await a.claim(() => post(url));
          const bytes = await read(response, mode); expect(bytes).toEqual(data);
          a.capture(bytes);
          await settle();
          expect(uploads.map(u => u.data)).toEqual([data]);
          expect(parentOf(data)).toBe(a.span.spanContext().spanId);
          expect(wireSpans).not.toContain(parentOf(data)); idle();
        });
      });
    }
    test(`an outer owner alone (the ElevenLabs marker, no adapter upload) on ${route}: nothing uploads, nothing is held`, async () => {
      const response = await context.with(ROOT_CONTEXT.setValue(OWNER, true), () => post(url));
      expect(await read(response, "reader")).toEqual(data);
      await settle(); expect(uploads).toHaveLength(0); idle();
    });
  }

  test.each(["policy", "no-policy"] as const)("adapter-owned transcription FormData (%s): the file path is skipped too", async p => {
    await context.with(outerContext(p), async () => {
      const a = adapter();
      const body = new FormData(); body.set("model", "whisper-1"); body.set("file", new Blob([openaiAudio], { type: "audio/wav" }), "synthetic.wav");
      await (await a.claim(() => fetch("https://api.openai.com/v1/audio/transcriptions", { method: "POST", body }))).json();
      a.capture(openaiAudio);
      await settle(); expect(uploads.map(u => u.data)).toEqual([openaiAudio]);
      expect(parentOf(openaiAudio)).toBe(a.span.spanContext().spanId); idle();
    });
  });

  test.each(["policy", "no-policy"] as const)("concurrent (%s): an owned OpenAI read and an unowned Deepgram read upload once each; the claim does not leak", async p => {
    await context.with(outerContext(p), async () => {
      const a = adapter();
      const [owned, unowned] = await Promise.all([
        (async () => { const bytes = await read(await a.claim(() => post(OPENAI_SPEECH)), "reader"); a.capture(bytes); return bytes; })(),
        (async () => read(await post(DEEPGRAM_SPEAK), "reader"))(),
      ]);
      expect(owned).toEqual(openaiAudio); expect(unowned).toEqual(deepgramAudio);
      await settle();
      expect(uploads).toHaveLength(2);
      expect(parentOf(openaiAudio)).toBe(a.span.spanContext().spanId);
      expect(wireSpans).toContain(parentOf(deepgramAudio)); idle();
    });
  });

  test.each(["policy", "no-policy"] as const)("real OpenAI SDK (%s): the claim around speech.create covers the later incremental read", async p => {
    const mod = require(process.env.ATT_OPENAI_MODULE || "openai"); const OpenAI = mod.default ?? mod.OpenAI;
    const sdk = new OpenAI({ apiKey: "synthetic-provider-key", fetch: (...args: Parameters<typeof fetch>) => globalThis.fetch(...args) });
    await context.with(outerContext(p), async () => {
      const a = adapter();
      const response = await a.claim(() => sdk.audio.speech.create({ model: "tts-1", voice: "alloy", input: "synthetic", response_format: "wav" }));
      const bytes = await read(response, "reader"); expect(bytes).toEqual(openaiAudio);
      a.capture(bytes);
      await settle(); expect(uploads.map(u => u.data)).toEqual([openaiAudio]);
      expect(parentOf(openaiAudio)).toBe(a.span.spanContext().spanId); idle();
    });
  });
});
