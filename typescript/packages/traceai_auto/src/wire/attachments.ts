import { createContextKey, trace, type Context, type Span } from "@opentelemetry/api";
import type {
  AttachmentCapturePolicy, AttachmentCorrelation, AttachmentManager, AttachmentPurpose, AttachmentStream,
} from "@traceai/fi-core/attachments";
import type { FetchMediaObserver, FetchMediaSink } from "./fetch-runtime";

const MEDIA_POLICY = createContextKey("traceai.auto.attachment_policy.v1");
/** Supply the integration's capture restrictions; inherited denials always win. */
export function withAttachmentCapturePolicy(parent: Context, policy: AttachmentCapturePolicy): Context {
  const previous = parent.getValue(MEDIA_POLICY) as AttachmentCapturePolicy | undefined;
  const merged = { ...policy };
  if (Object.values(policy).some(value => value !== undefined && typeof value !== "boolean")) merged.captureContent = false;
  for (const key of ["captureContent", "recordInputs", "recordOutputs", "requested"] as const) {
    if (previous?.[key] === false) merged[key] = false;
    else if (merged[key] === undefined) merged[key] = previous?.[key];
  }
  for (const key of ["muted", "temporaryChat", "modelInput", "containsInput", "containsOutput", "errorDetail", "newRecording"] as const) {
    if (previous?.[key]) merged[key] = true;
  }
  return parent.setValue(MEDIA_POLICY, Object.freeze(merged));
}

// `createContextKey` is `Symbol.for`, so adapters in other packages (for example the ElevenLabs
// instrumentation) can set the same marker without importing auto.
const MEDIA_OWNER = createContextKey("traceai.auto.attachment_owner.v1");
/** An adapter that captures media owns it independently of semantic span coverage. */
export function withAttachmentOwnership(parent: Context): Context { return parent.setValue(MEDIA_OWNER, true); }

const IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/webp"]);
const AUDIO_MIMES = new Set(["audio/wav", "audio/ogg", "audio/mpeg", "audio/mp4", "audio/webm", "audio/flac"]);
/** Consumed incremental audio is reserved up front; an object larger than this is discarded whole (too_large). */
export const WIRE_STREAM_MAX_BYTES = 4 * 1024 * 1024;
const WIRE_STREAM_FALLBACK_BYTES = 1024 * 1024;
/** Request JSON is parsed only from the wire path's own bounded copy, and only up to this size. */
const MAX_JSON_BYTES = 4 * 1024 * 1024;

type Operation = "speech" | "transcription" | "image_edit" | "image_generation" | "chat" | "responses";
/** Allowlisted protocol entries: exact HTTPS origin and path. A compatible path on another host is not authority to capture. */
export const WIRE_MEDIA_ROUTES: ReadonlyArray<Readonly<{ host: string; path: string; operation: Operation }>> = Object.freeze([
  { host: "api.openai.com", path: "/v1/audio/speech", operation: "speech" },
  { host: "api.openai.com", path: "/v1/audio/transcriptions", operation: "transcription" },
  { host: "api.openai.com", path: "/v1/audio/translations", operation: "transcription" },
  { host: "api.openai.com", path: "/v1/images/edits", operation: "image_edit" },
  { host: "api.openai.com", path: "/v1/images/variations", operation: "image_edit" },
  { host: "api.openai.com", path: "/v1/images/generations", operation: "image_generation" },
  { host: "api.openai.com", path: "/v1/chat/completions", operation: "chat" },
  { host: "api.openai.com", path: "/v1/responses", operation: "responses" },
  { host: "api.deepgram.com", path: "/v1/speak", operation: "speech" },
].map(route => Object.freeze(route)) as { host: string; path: string; operation: Operation }[]);
function operation(method: string, url: string): Operation | undefined {
  try {
    const u = new URL(url);
    if (method.toUpperCase() !== "POST" || u.protocol !== "https:" || u.username || u.password || (u.port && u.port !== "443")) return;
    return WIRE_MEDIA_ROUTES.find(route => route.host === u.hostname && route.path === u.pathname)?.operation;
  } catch { /* Unknown destinations never acquire media ownership. */ }
}
type OpenAIAdapter = typeof import("@traceai/openai").captureOpenAIAttachments;
let openAIAdapter: OpenAIAdapter | null | undefined;
/** Loaded only when attachments are active, so a disabled auto loads no extra instrumentation module. */
function captureOpenAIAttachments(...args: Parameters<OpenAIAdapter>): void {
  if (openAIAdapter === undefined) {
    try { openAIAdapter = (require("@traceai/openai") as typeof import("@traceai/openai")).captureOpenAIAttachments ?? null; }
    catch { openAIAdapter = null; }
  }
  // Without the shared provider adapter (an older @traceai/openai), JSON media is not captured: no private parser.
  if (typeof openAIAdapter === "function") openAIAdapter(...args);
}
function mediaType(value: string): string { return value.split(";")[0].trim().toLowerCase(); }
function modelSpan(parent: Context): Span | undefined {
  const span = trace.getSpan(parent);
  // Do not relink wire media to an unrelated ambient HTTP/tool/root span.
  const readable = span as (Span & { attributes?: Record<string, unknown>; span?: { attributes?: Record<string, unknown> } }) | undefined;
  const attributes = readable?.attributes ?? readable?.span?.attributes;
  const kind = attributes?.["gen_ai.span.kind"] ?? attributes?.["fi.span.kind"];
  return span?.isRecording() && ["LLM", "EMBEDDING"].includes(String(kind)) ? span : undefined;
}
/** True when the manager could admit anything at all. A disabled (default) manager makes auto behave exactly as before. */
export function attachmentsActive(manager: AttachmentManager | undefined): manager is AttachmentManager {
  try { return !!manager && manager.status().state !== "disabled"; } catch { return false; }
}
function parseJson(bytes: Uint8Array | undefined): unknown {
  if (!bytes || !bytes.byteLength || bytes.byteLength > MAX_JSON_BYTES) return undefined;
  try { return JSON.parse(new TextDecoder().decode(bytes)); } catch { return undefined; }
}
function imageFormat(value: unknown): "png" | "jpeg" | "webp" | undefined {
  return value === "png" || value === "jpeg" || value === "webp" ? value : undefined;
}

/**
 * Wire media observation for allowlisted protocol entries (A03/A04).
 *
 * - Capture happens only against a REAL span: a recording native model span whose integration supplied an explicit
 *   capture policy, or the wire span itself once it is emitted (`bind`). The ambient parent is never used.
 * - Every source passes `manager.preflight` (the full deny-wins lattice plus budget) BEFORE it is read, copied or
 *   decoded. JSON media goes through the OpenAI provider adapter (#682), so an SDK span and a wire observation of the
 *   same span and position share one occurrence.
 * - No reader, clone, tee, filesystem, URL retrieval or recorder: only values and chunks the application consumed.
 * - Before the wire span exists, nothing is retained: wire-only FormData and wire-only incremental reads are not
 *   captured (`correlation_unavailable`).
 */
export function wireAttachmentObserver(manager: AttachmentManager, parent: Context, method: string, url: string): FetchMediaObserver | undefined {
  if (parent.getValue(MEDIA_OWNER) || manager.status().state !== "ready") return;
  const op = operation(method, url);
  if (!op) return;
  const native = modelSpan(parent);
  const capture = parent.getValue(MEDIA_POLICY) as AttachmentCapturePolicy | undefined;
  // An external native instrumentor can have stricter per-call privacy than auto.
  // It must supply its policy, not silently inherit an unrelated manager opt-in.
  if (native && !capture) return;
  let correlation: AttachmentCorrelation | undefined = native ? manager.correlation(native) : undefined;
  let outputObserved = false, inputObserved = false, format: ReturnType<typeof imageFormat>;

  /** Preflight at the exact size, then attach. The source is touched only after admission. */
  const attachExact = (size: number, mimeType: string, direction: "input" | "output", purpose: AttachmentPurpose,
    source: () => { kind: "Bytes"; data: Uint8Array } | { kind: "Blob"; data: Blob }, sourceKind: "Bytes" | "Blob") => {
    if (!correlation || !(size > 0)) return;
    const ticket = manager.preflight(correlation, { mimeType, direction, purpose, maxBytes: size, sourceKind, capture: { ...capture, ...(direction === "input" ? { modelInput: true } : {}) } });
    if (!ticket.admitted) return;
    try { ticket.attach(source() as never); } catch { ticket.release(); }
  };
  const captureJsonInput = (body: unknown) => {
    if (inputObserved || !correlation || !body || typeof body !== "object") return;
    inputObserved = true;
    format = imageFormat((body as Record<string, unknown>).output_format);
    if (op === "chat" || op === "responses") captureOpenAIAttachments(manager, correlation, body, { operation: op, direction: "input", capture });
  };
  const formData = (body: FormData) => {
    let entries = 0;
    for (const [name, value] of body.entries()) {
      if (++entries > 64) break;
      if (typeof value === "string") { if (name === "output_format") format = imageFormat(value); continue; }
      const type = mediaType(value.type);
      if (op === "transcription" && name === "file" && AUDIO_MIMES.has(type)) {
        attachExact(value.size, type, "input", "model_input", () => ({ kind: "Blob", data: value }), "Blob");
      } else if (op === "image_edit" && ["image", "image[]", "mask"].includes(name) && IMAGE_MIMES.has(type)) {
        attachExact(value.size, type, "input", "model_input", () => ({ kind: "Blob", data: value }), "Blob");
      }
    }
  };
  return {
    bind(span, request) {
      // The emitter supplies its actual identity before ending, never a later ambient span.
      if (correlation) return;
      correlation = manager.correlation(span);
      // JSON input uses the wire path's own bounded copy; nothing was retained for media before this point.
      if (op === "chat" || op === "responses" || op === "image_generation") captureJsonInput(parseJson(request?.body));
    },
    request(body) {
      // Wire-only: the span does not exist yet, so FormData is not retained until a late span (fail closed).
      if (!correlation) return;
      if (body instanceof FormData && (op === "transcription" || op === "image_edit")) formData(body);
      else if (typeof body === "string" && (op === "chat" || op === "responses" || op === "image_generation") && body.length <= MAX_JSON_BYTES) {
        try { captureJsonInput(JSON.parse(body)); } catch { /* Not JSON: nothing to capture. */ }
      }
    },
    consumed(mimeType, successful, contentLength): FetchMediaSink | undefined {
      // Incremental reads need the target before the first chunk: only a native span qualifies (A04).
      const type = mediaType(mimeType);
      if (outputObserved || !correlation || !successful || op !== "speech" || !AUDIO_MIMES.has(type)) return;
      const open = (maxBytes: number) => manager.startAttachmentStream(correlation!, { mimeType: type, direction: "output", purpose: "model_output", maxBytes, capture });
      const exact = contentLength !== undefined && contentLength > 0 && contentLength <= WIRE_STREAM_MAX_BYTES ? contentLength : undefined;
      let stream: AttachmentStream = open(exact ?? WIRE_STREAM_MAX_BYTES);
      // The negotiated upload limit or the owned budget can be below the default reservation.
      if (!exact && (stream.reason === "too_large" || stream.reason === "queue_full")) stream = open(WIRE_STREAM_FALLBACK_BYTES);
      if (stream.reason !== "queued") return;
      outputObserved = true;
      return { write: chunk => stream.tryWrite(chunk), end: () => { stream.finish(); }, abort: () => stream.abort("incomplete") };
    },
    materialized(value, methodName, mimeType, successful) {
      if (outputObserved || !correlation || !successful) return;
      const type = mediaType(mimeType);
      if (op === "speech" && AUDIO_MIMES.has(type)) {
        if (methodName === "blob" && value instanceof Blob) {
          outputObserved = true;
          attachExact(value.size, type, "output", "model_output", () => ({ kind: "Blob", data: value }), "Blob");
        } else if (methodName === "arrayBuffer" && value instanceof ArrayBuffer) {
          outputObserved = true;
          // The view (and the core's synchronous copy) exists only after admission.
          attachExact(value.byteLength, type, "output", "model_output", () => ({ kind: "Bytes", data: new Uint8Array(value) }), "Bytes");
        }
      } else if (methodName === "json" && type === "application/json" && (op === "image_generation" || op === "image_edit" || op === "responses")) {
        outputObserved = true;
        // Typed provider adapter only (#682): no recursive scraping, no URL retrieval, decode after admission.
        captureOpenAIAttachments(manager, correlation, value, { operation: op === "responses" ? "responses" : "images", direction: "output", capture, imageFormat: format });
      }
    },
  };
}
