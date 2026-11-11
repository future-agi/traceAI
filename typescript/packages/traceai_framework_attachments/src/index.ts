import type { Span } from "@opentelemetry/api";
import type { TraceConfigOptions } from "@traceai/fi-core";
import type { AttachmentCapturePolicy, AttachmentManager, AttachmentOptions } from "@traceai/fi-core/attachments";

export type MediaDialect = "langchain" | "llamaindex" | "vercel" | "genkit";
export interface FrameworkAttachmentOptions {
  /** The configured provider's manager; adapters never create another uploader. */
  attachments?: AttachmentManager;
  traceConfig?: TraceConfigOptions;
  capture?: AttachmentCapturePolicy;
}

/** Data descriptors only. Media observation must not invoke application getters/toJSON. */
export function mediaField(value: unknown, key: string): unknown {
  if (!value || typeof value !== "object") return undefined;
  const field = Object.getOwnPropertyDescriptor(value, key);
  return field && "value" in field ? field.value : undefined;
}

// A synchronous callback may decode at most one MiB per part. Larger strings are
// unsupported here, not a reason to enlarge the core's bounded capture budget.
const MAX_DECODED = 1024 * 1024;
const MAX_ENCODED = Math.ceil(MAX_DECODED / 3) * 4;
const MAX_PARTS = 64;
const seen = new WeakMap<AttachmentManager, WeakMap<Span, Set<string>>>();

function dataUriMime(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  return /^data:([a-zA-Z0-9/+.-]+);base64,/.exec(value.slice(0, 96))?.[1];
}
function decode(value: unknown): Uint8Array {
  if (value instanceof Uint8Array) return value;
  // No implicit ArrayBuffer conversion, stream reads, paths or remote retrieval.
  if (typeof value !== "string") throw new Error("unsupported media source");
  const prefix = /^data:[a-zA-Z0-9/+.-]+;base64,/.exec(value.slice(0, 96));
  const length = value.length - (prefix?.[0].length ?? 0);
  if (!length || length > MAX_ENCODED || length % 4 !== 0) throw new Error("unsupported media size");
  const encoded = prefix ? value.slice(prefix[0].length) : value;
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) throw new Error("invalid media encoding");
  const binary = atob(encoded);
  if (binary.length > MAX_DECODED) throw new Error("unsupported media size");
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function normalized(part: unknown, dialect: MediaDialect): { data: unknown; mime: string } | undefined {
  const type = mediaField(part, "type");
  let data: unknown;
  let mime: unknown;
  if (dialect === "genkit") {
    const media = mediaField(part, "media");
    data = mediaField(media, "url");
    // Genkit URLs are only captured when they contain inline data. Never fetch.
    const uriMime = dataUriMime(data);
    if (!uriMime) return;
    mime = mediaField(media, "contentType") ?? uriMime;
  } else if (dialect === "vercel") {
    if (type === "image") { data = mediaField(part, "image"); mime = mediaField(part, "mediaType") ?? mediaField(part, "mimeType"); }
    else if (type === "file") {
      data = mediaField(part, "data"); mime = mediaField(part, "mediaType") ?? mediaField(part, "mimeType");
      // AI SDK v7 end events retain DefaultGeneratedFile backing values. Read
      // only data descriptors, never its decoding getters or provider metadata.
      if (data === undefined) {
        const file = mediaField(part, "file");
        data = mediaField(file, "uint8ArrayData") ?? mediaField(file, "base64Data");
        mime = mediaField(file, "mediaType");
      }
    }
    else if (type === "image-data" || type === "file-data") { data = mediaField(part, "data"); mime = mediaField(part, "mediaType"); }
    else return;
  } else {
    if (type === "image_url") {
      const image = mediaField(part, "image_url");
      data = typeof image === "string" ? image : mediaField(image, "url");
      mime = dataUriMime(data);
      if (!mime) return;
    } else if ((type === "image" || type === "audio" || type === "file") && mediaField(part, "source_type") === "base64") {
      data = mediaField(part, "data"); mime = mediaField(part, "mime_type");
    } else return;
  }
  const uriMime = dataUriMime(data);
  if (uriMime && mime !== undefined && mime !== uriMime) return;
  mime ??= uriMime;
  if (typeof mime !== "string" || mime.length > 96) return;
  return { data, mime };
}

function locallyDenied(config: TraceConfigOptions | undefined, options: AttachmentOptions): boolean {
  if (!config) return false;
  const input = options.direction !== "output" || options.capture?.modelInput || options.capture?.containsInput;
  const output = options.direction !== "input" || options.capture?.containsOutput;
  // Required text PII handling cannot sanitize opaque media. No sanitizer claim.
  return !!(config.piiRedaction || (input && (config.hideInputs || config.hideInputMessages || config.hideInputText ||
    (options.mimeType.startsWith("image/") && config.hideInputImages))) ||
    (output && (config.hideOutputs || config.hideOutputMessages || config.hideOutputText)));
}

/**
 * Observe only explicit typed content arrays at an original-value boundary.
 * No recursive scraping, uploads from serialized telemetry, or caller mutation.
 * The source getter runs synchronously INSIDE core admission, never before it.
 */
export function captureMediaParts(binding: FrameworkAttachmentOptions, span: Span, parts: unknown,
  dialect: MediaDialect, options: Omit<AttachmentOptions, "mimeType" | "partIndex">): void {
  if (!binding.attachments || !Array.isArray(parts)) return;
  try {
    const manager = binding.attachments;
    let spans = seen.get(manager);
    if (!spans) { spans = new WeakMap(); seen.set(manager, spans); }
    let positions = spans.get(span);
    if (!positions) { positions = new Set(); spans.set(span, positions); }
    for (let i = 0; i < Math.min(parts.length, MAX_PARTS); i++) {
      if (positions.size >= MAX_PARTS) break;
      const part = mediaField(parts, String(i));
      const media = normalized(part, dialect);
      if (!media) continue;
      const key = `${options.direction}/${options.messageIndex ?? 0}/${i}/${options.purpose ?? ""}`;
      if (positions.has(key)) continue;
      const capture = { ...binding.capture, ...options.capture };
      // A per-part allowance must not relax a binding-level denial.
      for (const restriction of ["captureContent", "recordInputs", "recordOutputs", "requested"] as const) {
        if (binding.capture?.[restriction] === false) capture[restriction] = false;
      }
      for (const restriction of ["muted", "temporaryChat", "containsInput", "containsOutput", "modelInput", "errorDetail"] as const) {
        if (binding.capture?.[restriction]) capture[restriction] = true;
      }
      const attachmentOptions: AttachmentOptions = { ...options, mimeType: media.mime, partIndex: i, capture };
      if (locallyDenied(binding.traceConfig, attachmentOptions)) continue;
      // Lazy decode also ensures disabled, PII, unsampled and full-queue captures
      // do not allocate decoded media. Core owns the detached mutable-byte copy.
      const result = manager.attach(span, { kind: "Bytes", get data() { return decode(media.data); } }, attachmentOptions);
      if (result.admission === "queued") positions.add(key);
    }
  } catch { /* Unsupported/malformed media must never affect framework execution. */ }
}

/** Bounded message traversal, never an arbitrary object walk. */
export function captureMediaMessages(binding: FrameworkAttachmentOptions, span: Span, messages: unknown,
  dialect: MediaDialect, direction: "input" | "output"): void {
  if (!binding.attachments || !Array.isArray(messages)) return;
  try {
    for (let i = 0; i < Math.min(messages.length, MAX_PARTS); i++) {
      const message = mediaField(messages, String(i));
      // LangChain BaseTracer may retain its documented serialized BaseMessage form.
      const content = mediaField(message, "content") ?? (dialect === "langchain" ? mediaField(mediaField(message, "kwargs"), "content") : undefined);
      const nativeRole = mediaField(message, "role");
      const role = ["user", "assistant", "tool", "system"].includes(nativeRole as string) ? nativeRole as "user" | "assistant" | "tool" | "system" : "unknown";
      captureMediaParts(binding, span, content, dialect, { direction, messageIndex: i, role,
        purpose: direction === "input" ? "model_input" : "model_output", capture: { modelInput: direction === "input" } });
    }
  } catch { /* No diagnostics containing source data. */ }
}
