import type { TraceConfigOptions } from "@traceai/fi-core";
import type { AttachmentManager, AttachmentOptions, AttachmentSpan, AttachmentCorrelation, AttachmentCapturePolicy } from "@traceai/fi-core/attachments";

/** Declarative handoff to auto/wire owners; this is not a native/wire hookup. */
export const openAIAttachmentCapabilities = Object.freeze({
  schemaVersion: 1,
  operations: ["chat.completions.create.input", "responses.create.input", "responses.create.output", "responses.stream.output", "images.generate.output"] as const,
  sources: ["inline_base64"] as const,
  /** Fail closed: no capture is attempted for these. Streamed Responses output is captured only from the
   * terminal `response.completed` event the application consumed; partial (preview) images never are. */
  unsupported: ["remote_url", "provider_file", "partial_image", "images.generate.stream", "images.edit", "images.createVariation", "speech"] as const,
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

type Parsed = { mimeType: string; encoded: string; bound: number };
const MAX_ENCODED = 4 * Math.ceil(openAIAttachmentCapabilities.maxInlineBytes / 3);

/** Structural parse only: no decode or allocation beyond the string the application already holds.
 * Returns the candidate MIME named by a data URI (or the typed MIME for raw base64) and the exact decoded length.
 */
function parse(value: unknown, mimeTypes: string[], raw: boolean): Parsed | undefined {
  if (typeof value !== "string") return undefined;
  let mimeType = mimeTypes[0], start = 0;
  if (!raw) {
    const comma = value.indexOf(",");
    const header = comma > 5 && comma < 128 ? value.slice(0, comma) : "";
    const match = /^data:([a-z]+\/[a-z0-9.+-]+);base64$/.exec(header);
    if (!match || !mimeTypes.includes(match[1])) return undefined;
    mimeType = match[1]; start = comma + 1;
  }
  const length = value.length - start;
  // Bound allocation BEFORE slicing/decoding.
  if (length <= 0 || length > MAX_ENCODED || length % 4 !== 0) return undefined;
  const encoded = value.slice(start);
  const padding = encoded.endsWith("==") ? 2 : encoded.endsWith("=") ? 1 : 0;
  return { mimeType, encoded, bound: (length / 4) * 3 - padding };
}

/** Strict decode after admission. Padding and alphabet are strict; the ticket's budget is a hard bound. */
function decode(encoded: string, maxBytes: number): Uint8Array | undefined {
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) return undefined;
  const bytes = Buffer.from(encoded, "base64");
  if (!bytes.length || bytes.length > maxBytes || bytes.length > openAIAttachmentCapabilities.maxInlineBytes || bytes.toString("base64") !== encoded) return undefined;
  return bytes;
}

const key = (options: Pick<OpenAIAttachmentCaptureOptions, "operation" | "direction">, messageIndex: number, partIndex: number) =>
  `${options.operation}/${options.direction}/${messageIndex}/${partIndex}`;

/** Typed, synchronous observation only. No provider downloads or stream consumption.
 * Pass the SAME emitted identity to SDK and wire observations: a second observation of a position
 * returns the first occurrence instead of uploading again.
 * Denied sources are never read: every part is preflighted (deny-wins, budget reserved) before its value is
 * read or decoded, and an admitted ticket that is not used is released at once.
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
      // The source is read at most once, and only after a preflight admitted this modality (design section 1).
      let source: Parsed | undefined, sourceRead = false;
      const parsed = () => { if (!sourceRead) { sourceRead = true; source = parse(read(), mimeTypes, raw); } return source; };
      const preflight = (mimeType: string) => {
        const request = { mimeType, direction: options.direction, purpose, capture: {
          ...capture,
          ...(options.direction === "input" && mimeType.startsWith("image/") && options.traceConfig?.hideInputImages ? { captureContent: false } : {}),
        } };
        let ticket = manager.preflight(correlation, { ...request, maxBytes: openAIAttachmentCapabilities.maxInlineBytes });
        // too_large (negotiated limit below the cap) and queue_full (owned budget nearly used) are evaluated after every
        // privacy, correlation and sampling step, so sizing the source exactly and retrying once is safe here.
        if (ticket.reason === "too_large" || ticket.reason === "queue_full") {
          const bound = parsed()?.bound ?? 0;
          if (bound > 0) ticket = manager.preflight(correlation, { ...request, maxBytes: bound });
        }
        return ticket;
      };
      let ticket = preflight(mimeTypes[0]);
      if (!ticket.admitted) return;
      const found = parsed();
      if (!found) { ticket.release(); return; }
      // A data URI names a sibling MIME of the same modality: re-admit under the exact type before decoding.
      if (found.mimeType !== mimeTypes[0]) {
        ticket.release();
        ticket = preflight(found.mimeType);
        if (!ticket.admitted) return;
      }
      const bytes = decode(found.encoded, ticket.maxBytes);
      if (!bytes) { ticket.release(); return; }
      const handle = ticket.attach({ kind: "Bytes", data: bytes }, { messageIndex, partIndex, role, ...(toolCallId ? { toolCallId } : {}) });
      if (handle.admission !== "queued") return;
      const occurrence = Object.freeze({ occurrenceId: handle.occurrenceId, mimeType: found.mimeType });
      let positions = spans!.get(spanKey);
      if (!positions) {
        if (spans!.size >= MAX_SPANS) spans!.delete(spans!.keys().next().value!);
        positions = new Map(); spans!.set(spanKey, positions);
      }
      if (positions.size < MAX_PARTS) positions.set(position, occurrence);
      captured.set(position, occurrence);
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
