import { createContextKey, trace, type Context, type Span } from "@opentelemetry/api";
import type { AttachmentCorrelation, AttachmentManager, AttachmentOptions, AttachmentCapturePolicy } from "@traceai/fi-core/attachments";
import type { FetchMediaObserver } from "./fetch-runtime";

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

const MEDIA_OWNER = createContextKey("traceai.auto.attachment_owner.v1");
/** An adapter that captures media owns it independently of semantic span coverage. */
export function withAttachmentOwnership(parent: Context): Context { return parent.setValue(MEDIA_OWNER, true); }

const IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/webp"]);
const AUDIO_MIMES = new Set(["audio/wav", "audio/ogg", "audio/mpeg", "audio/mp4", "audio/webm", "audio/flac"]);
// Bounded transient decoder scratch, not a second media queue.
const MAX_BASE64 = 4 * Math.ceil(256 * 1024 / 3);

type Operation = "speech" | "transcription" | "image_edit" | "image_generation";
function operation(method: string, url: string): Operation | undefined {
  try {
    const u = new URL(url);
    // A compatible path on an unknown host is not authority to capture media.
    if (method.toUpperCase() !== "POST" || u.protocol !== "https:" || u.username || u.password || (u.port && u.port !== "443")) return;
    if (u.hostname === "api.openai.com") {
      if (u.pathname === "/v1/audio/speech") return "speech";
      if (["/v1/audio/transcriptions", "/v1/audio/translations"].includes(u.pathname)) return "transcription";
      if (["/v1/images/edits", "/v1/images/variations"].includes(u.pathname)) return "image_edit";
      if (u.pathname === "/v1/images/generations") return "image_generation";
    }
    if (u.hostname === "api.deepgram.com" && u.pathname === "/v1/speak") return "speech";
  } catch { /* Unknown destinations never acquire media ownership. */ }
}
function modelSpan(parent: Context): Span | undefined {
  const span = trace.getSpan(parent);
  // Do not relink wire media to an unrelated ambient HTTP/tool/root span.
  const readable = span as (Span & { attributes?: Record<string, unknown>; span?: { attributes?: Record<string, unknown> } }) | undefined;
  const attributes = readable?.attributes ?? readable?.span?.attributes;
  const kind = attributes?.["gen_ai.span.kind"] ?? attributes?.["fi.span.kind"];
  return span?.isRecording() && ["LLM", "EMBEDDING"].includes(String(kind)) ? span : undefined;
}

/**
 * Observe only immutable inputs or results the caller has already materialized.
 * No reader, clone, tee, filesystem, URL retrieval, recorder or private uploader.
 * Incremental/encoded streaming waits for core's admission/reservation API.
 */
export function wireAttachmentObserver(manager: AttachmentManager, parent: Context, method: string, url: string): FetchMediaObserver | undefined {
  if (parent.getValue(MEDIA_OWNER) || manager.status().state !== "queued") return;
  const op = operation(method, url);
  if (!op) return;
  let correlation: AttachmentCorrelation | undefined;
  const native = modelSpan(parent);
  const capture = parent.getValue(MEDIA_POLICY) as AttachmentCapturePolicy | undefined;
  // An external native instrumentor can have stricter per-call privacy than auto.
  // It must supply its policy, not silently inherit an unrelated manager opt-in.
  if (native && !capture) return;
  if (native) correlation = manager.correlation(native);
  let outputObserved = false;
  const attachBlob = (blob: Blob, options: AttachmentOptions) => {
    if (correlation) manager.attach(correlation, { kind: "Blob", data: blob }, { ...options, capture: { ...capture, ...options.capture } });
  };
  return {
    bind(span) {
      // The emitter supplies its actual identity before ending, never a later ambient span.
      if (!correlation) correlation = manager.correlation(span);
    },
    request(body) {
      // A wire-only span is assigned late. Do not retain a pre-admission FormData.
      if (!correlation || !(body instanceof FormData)) return;
      let entries = 0;
      for (const [name, value] of body.entries()) {
        if (++entries > 64) break;
        if (!(value instanceof Blob)) continue;
        if (op === "transcription" && name === "file" && AUDIO_MIMES.has(value.type)) {
          attachBlob(value, { mimeType: value.type, direction: "input", purpose: "model_input" });
        } else if (op === "image_edit" && ["image", "image[]", "mask"].includes(name) && IMAGE_MIMES.has(value.type)) {
          attachBlob(value, { mimeType: value.type, direction: "input", purpose: "model_input", capture: { modelInput: true } });
        }
      }
    },
    materialized(value, methodName, mimeType, successful) {
      if (outputObserved || !correlation || !successful) return;
      if (op === "speech" && AUDIO_MIMES.has(mimeType)) {
        if (methodName === "blob" && value instanceof Blob) {
          outputObserved = true;
          attachBlob(value, { mimeType, direction: "output", purpose: "model_output" });
        } else if (methodName === "arrayBuffer" && value instanceof ArrayBuffer) {
          outputObserved = true;
          // No copy/view is constructed until the shared policy gate permits access.
          manager.attach(correlation, { kind: "Bytes", get data() { return new Uint8Array(value); } },
            { mimeType, direction: "output", purpose: "model_output", capture });
        }
      } else if ((op === "image_generation" || op === "image_edit") && methodName === "json" && mimeType === "application/json") {
        outputObserved = true;
        // Typed OpenAI response only, never recursively scrape arbitrary JSON.
        if (!value || typeof value !== "object") return;
        const images: unknown = Object.getOwnPropertyDescriptor(value, "data")?.value;
        if (!Array.isArray(images)) return;
        for (let index = 0; index < Math.min(images.length, 64); index++) {
          const item: unknown = images[index];
          if (!item || typeof item !== "object") continue;
          const encoded: unknown = Object.getOwnPropertyDescriptor(item, "b64_json")?.value;
          if (typeof encoded !== "string") continue; // Remote URLs are NOT retrieved.
          manager.attach(correlation, { kind: "Bytes", get data() {
            // Decoding and byte inspection are behind server/local privacy admission.
            if (!encoded.length || encoded.length > MAX_BASE64 || encoded.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) return new Uint8Array();
            const bytes = Buffer.from(encoded, "base64");
            // This operation only qualifies PNG. Other formats remain unsupported.
            return bytes.length <= 256 * 1024 && bytes.length >= 8 && bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])) ? bytes : new Uint8Array();
          } }, { mimeType: "image/png", direction: "output", purpose: "model_output", capture });
        }
      }
    },
  };
}
