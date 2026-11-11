import type { TraceConfigOptions } from "@traceai/fi-core";
import type { AttachmentManager, AttachmentOptions, AttachmentSpan, AttachmentCorrelation, AttachmentCapturePolicy } from "@traceai/fi-core/attachments";

/** Declarative handoff to auto/wire owners; this is not a native/wire hookup. */
export const openAIAttachmentCapabilities = Object.freeze({
  schemaVersion: 1,
  operations: ["chat.completions.create.input", "responses.create.input", "responses.create.output", "images.generate.output"] as const,
  sources: ["inline_base64"] as const,
  /** Fail closed: no capture is attempted for these. */
  unsupported: ["remote_url", "provider_file", "streamed_output", "partial_image", "images.edit", "images.createVariation", "speech"] as const,
  maxInlineBytes: 256 * 1024,
});

/** Admitted media positions for one observation: position key -> occurrence. */
export type CapturedMedia = ReadonlyMap<string, Readonly<{ occurrenceId: string; mimeType: string }>>;
type Captured = Map<string, Readonly<{ occurrenceId: string; mimeType: string }>>;
type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue => value !== null && typeof value === "object" ? value as RecordValue : {};
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
const MAX_PARTS = 256;
const MAX_SPANS = 1024;
const EMPTY: CapturedMedia = new Map();
const IMAGES = ["image/png", "image/jpeg", "image/webp"];
const FILES = ["application/pdf", "application/octet-stream"];
// Shared across CJS/ESM copies. Retains only admitted span IDs, positions and occurrence IDs, never sources.
const registryKey = Symbol.for("@traceai/openai/attachment-observations/v1");
type Registry = WeakMap<AttachmentManager, Map<string, Captured>>;
const host = globalThis as typeof globalThis & { [registryKey]?: Registry };
const observations = (host[registryKey] ??= new WeakMap());

export interface OpenAIAttachmentCaptureOptions {
  operation: "chat" | "responses" | "images";
  direction: "input" | "output";
  traceConfig?: TraceConfigOptions;
  capture?: AttachmentCapturePolicy;
  /** The Images request's typed output_format; never inferred from filenames. */
  imageFormat?: "png" | "jpeg" | "webp";
}

/** The local instrumentor's restrictions; the shared manager applies its own policy as well (deny wins). */
function restrictions(options: OpenAIAttachmentCaptureOptions): AttachmentCapturePolicy {
  const config = options.traceConfig ?? {};
  const input = options.direction === "input";
  const hidden = input
    ? config.hideInputs || config.hideInputMessages || config.hideInputText
    : config.hideOutputs || config.hideOutputMessages || config.hideOutputText;
  return { ...options.capture,
    ...(hidden || config.piiRedaction ? { captureContent: false } : {}),
    ...(input ? { modelInput: true } : {}),
  };
}

function decode(value: unknown, mimeType: string, raw: boolean): Uint8Array {
  if (typeof value !== "string") return new Uint8Array();
  const prefix = `data:${mimeType};base64,`;
  if (!raw && !value.startsWith(prefix)) return new Uint8Array();
  // Bound allocation BEFORE slicing/decoding. Padding and alphabet are strict.
  const start = raw ? 0 : prefix.length;
  const length = value.length - start;
  if (length <= 0 || length > 4 * Math.ceil(openAIAttachmentCapabilities.maxInlineBytes / 3) || length % 4 !== 0) return new Uint8Array();
  const encoded = value.slice(start);
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) return new Uint8Array();
  const bytes = Buffer.from(encoded, "base64");
  if (bytes.length > openAIAttachmentCapabilities.maxInlineBytes || bytes.toString("base64") !== encoded) return new Uint8Array();
  return bytes;
}

const key = (options: Pick<OpenAIAttachmentCaptureOptions, "operation" | "direction">, messageIndex: number, partIndex: number) =>
  `${options.operation}/${options.direction}/${messageIndex}/${partIndex}`;

/** Typed, synchronous observation only. No provider downloads or stream consumption.
 * Pass the SAME emitted identity to SDK and wire observations: a second observation of a position
 * returns the first occurrence instead of uploading again.
 * Denied sources are never read: the shared manager calls the getter only after admission.
 * Returns the admitted positions so the caller can project them out of span telemetry.
 */
export function captureOpenAIAttachments(manager: AttachmentManager | undefined,
  span: AttachmentSpan | AttachmentCorrelation, payload: unknown, options: OpenAIAttachmentCaptureOptions): CapturedMedia {
  if (!manager) return EMPTY;
  const captured: Captured = new Map();
  try {
    const correlation = "snapshot" in span ? span : manager.correlation(span);
    const identity = correlation.snapshot();
    if (!identity?.sampled) return EMPTY;
    let spans = observations.get(manager);
    if (!spans) { spans = new Map(); observations.set(manager, spans); }
    const spanKey = `${identity.traceId}/${identity.spanId}`;
    const capture = restrictions(options);
    let visited = 0;
    const attach = (read: () => unknown, mimeTypes: string[], messageIndex: number, partIndex: number,
      purpose: AttachmentOptions["purpose"], toolCallId?: string, raw = false, role: AttachmentOptions["role"] = "user") => {
      if (++visited > MAX_PARTS) return;
      const position = key(options, messageIndex, partIndex);
      const previous = spans!.get(spanKey)?.get(position);
      if (previous) { captured.set(position, previous); return; }
      for (const mimeType of mimeTypes) {
        const handle = manager.attach(correlation, { kind: "Bytes", get data() { return decode(read(), mimeType, raw); } },
          { mimeType, direction: options.direction, purpose, messageIndex, partIndex, role,
            ...(toolCallId ? { toolCallId } : {}), capture: {
              ...capture,
              ...(options.direction === "input" && mimeType.startsWith("image/") && options.traceConfig?.hideInputImages ? { captureContent: false } : {}),
            } });
        if (handle.admission === "queued") {
          const occurrence = Object.freeze({ occurrenceId: handle.occurrenceId, mimeType });
          let positions = spans!.get(spanKey);
          if (!positions) {
            if (spans!.size >= MAX_SPANS) spans!.delete(spans!.keys().next().value!);
            positions = new Map(); spans!.set(spanKey, positions);
          }
          if (positions.size < MAX_PARTS) positions.set(position, occurrence);
          captured.set(position, occurrence);
          return;
        }
        // Only a non-matching data-URI type (decoded as empty) tries the next candidate. Denials stop.
        if (handle.admission !== "too_large") return;
      }
    };
    const toolCallId = (value: unknown) => typeof value === "string" && /^[a-zA-Z0-9_-]{1,128}$/.test(value) ? value : undefined;
    const parts = (content: unknown, messageIndex: number, role: AttachmentOptions["role"]) => {
      list(content).slice(0, MAX_PARTS).forEach((value, partIndex) => {
        const part = record(value);
        if (part.type === "image_url") attach(() => record(part.image_url).url, IMAGES, messageIndex, partIndex, "model_input", undefined, false, role);
        else if (part.type === "input_image") attach(() => part.image_url, IMAGES, messageIndex, partIndex, "model_input", undefined, false, role);
        else if (part.type === "input_file") attach(() => part.file_data, FILES, messageIndex, partIndex, "document", undefined, false, role);
        else if (part.type === "file") attach(() => record(part.file).file_data, FILES, messageIndex, partIndex, "document", undefined, false, role);
      });
    };
    const body = record(payload);
    if (options.direction === "output" && body.error) return EMPTY;
    if (options.direction === "input") {
      const messages = options.operation === "chat" ? body.messages : options.operation === "responses" ? body.input : undefined;
      list(messages).slice(0, MAX_PARTS).forEach((value, messageIndex) => {
        const message = record(value);
        if (message.type === "computer_call_output") {
          const output = record(message.output);
          // A screenshot is model-bound input. Its call id keeps the causal link to the computer_call.
          if (output.type === "computer_screenshot") attach(() => output.image_url, IMAGES, messageIndex, 0, "computer_screenshot",
            toolCallId(message.call_id), false, "tool");
        } else if (!message.type || message.type === "message") {
          const role = ["user", "assistant", "tool", "system"].includes(String(message.role)) ? message.role as AttachmentOptions["role"] : "unknown";
          parts(message.content, messageIndex, role);
        }
      });
    } else if (options.operation === "images") {
      // The Images API defaults to PNG. The typed request/response format wins where provided.
      const format = body.output_format ?? options.imageFormat;
      const mime = format === "jpeg" ? "image/jpeg" : format === "webp" ? "image/webp" : "image/png";
      list(body.data).slice(0, MAX_PARTS).forEach((value, index) => {
        const item = record(value);
        attach(() => item.b64_json, [mime], index, 0, "model_output", undefined, true, "assistant");
      });
    } else if (options.operation === "responses") {
      list(body.output).slice(0, MAX_PARTS).forEach((value, index) => {
        const item = record(value);
        if (item.type === "image_generation_call" && item.status === "completed") {
          const mime = item.output_format === "jpeg" ? "image/jpeg" : item.output_format === "webp" ? "image/webp" : "image/png";
          attach(() => item.result, [mime], index, 0, "model_output", undefined, true, "assistant");
        }
      });
    }
  } catch { /* Instrumentation must not throw or log source-bearing errors. */ }
  return captured;
}

const uri = (occurrence: { occurrenceId: string }) => `fi-attachment:${occurrence.occurrenceId}`;

/** Telemetry-only projection. ONLY admitted positions are replaced with their `fi-attachment:` occurrence
 * URI, so uploaded bytes are not exported twice. Everything else is returned exactly as before, and the
 * request passed to the SDK is never mutated.
 */
export function projectOpenAIMedia(payload: unknown, captured: CapturedMedia,
  operation: "chat" | "responses", direction: "input" | "output"): any {
  if (!captured.size) return payload;
  try {
    const body = record(payload);
    const field = direction === "input" ? operation === "chat" ? "messages" : "input" : "output";
    if (!Array.isArray(body[field])) return payload;
    const at = (messageIndex: number, partIndex: number) => captured.get(key({ operation, direction }, messageIndex, partIndex));
    return { ...body, [field]: list(body[field]).map((value, messageIndex) => {
      const message = record(value);
      if (message.type === "computer_call_output") {
        const occurrence = at(messageIndex, 0);
        return occurrence ? { ...message, output: { ...record(message.output), image_url: uri(occurrence) } } : value;
      }
      if (message.type === "image_generation_call") {
        const occurrence = at(messageIndex, 0);
        return occurrence ? { ...message, result: uri(occurrence) } : value;
      }
      if (!Array.isArray(message.content)) return value;
      return { ...message, content: message.content.map((value, partIndex) => {
        const occurrence = at(messageIndex, partIndex);
        if (!occurrence) return value;
        const part = record(value);
        if (part.type === "image_url") return { ...part, image_url: { ...record(part.image_url), url: uri(occurrence) } };
        if (part.type === "input_image") return { ...part, image_url: uri(occurrence) };
        if (part.type === "input_file") return { ...part, file_data: uri(occurrence) };
        if (part.type === "file") return { ...part, file: { ...record(part.file), file_data: uri(occurrence) } };
        return value;
      }) };
    }) };
  } catch { return payload; }
}
