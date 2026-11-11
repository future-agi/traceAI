import type { TraceConfigOptions } from "@traceai/fi-core";
import type { AttachmentManager, AttachmentOptions, AttachmentSpan, AttachmentCorrelation, AttachmentCapturePolicy } from "@traceai/fi-core/attachments";

/** Declarative handoff to auto/wire owners; this is not a native/wire hookup. */
export const openAIAttachmentCapabilities = Object.freeze({
  schemaVersion: 1,
  operations: ["chat.completions.create.input", "responses.create.input", "responses.create.output", "images.generate.output"] as const,
  sources: ["inline_base64"] as const,
  unsupported: ["remote_url", "provider_file", "image_edit_stream", "speech", "partial_image"] as const,
  maxInlineBytes: 256 * 1024,
});

type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue => value !== null && typeof value === "object" ? value as RecordValue : {};
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
const MAX_PARTS = 256;
// Shared across CJS/ESM copies. Retain only admitted span IDs and positional keys, never sources.
const registryKey = Symbol.for("@traceai/openai/attachment-observations/v1");
type Registry = WeakMap<AttachmentManager, Map<string, Set<string>>>;
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

/** Typed, synchronous observation only. No provider downloads or background stream consumption.
 * Pass the SAME emitted identity to SDK and wire observations to deduplicate positions.
 * Denied sources are never read: the shared manager calls the getter only after admission.
 */
export function captureOpenAIAttachments(manager: AttachmentManager | undefined,
  span: AttachmentSpan | AttachmentCorrelation, payload: unknown, options: OpenAIAttachmentCaptureOptions): void {
  if (!manager) return;
  try {
    const correlation = "snapshot" in span ? span : manager.correlation(span);
    const identity = correlation.snapshot();
    if (!identity?.sampled) return;
    let spans = observations.get(manager);
    if (!spans) { spans = new Map(); observations.set(manager, spans); }
    const spanKey = `${identity.traceId}/${identity.spanId}`;
    const capture = restrictions(options);
    let visited = 0;
    const attach = (read: () => unknown, mimeTypes: string[], messageIndex: number, partIndex: number,
      purpose: AttachmentOptions["purpose"], toolCallId?: string, raw = false, role: AttachmentOptions["role"] = "user") => {
      if (++visited > MAX_PARTS) return;
      const key = `${options.operation}/${options.direction}/${messageIndex}/${partIndex}`;
      if (spans!.get(spanKey)?.has(key)) return;
      for (const mimeType of mimeTypes) {
        const handle = manager.attach(correlation, { kind: "Bytes", get data() {
          const value = read();
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
        } }, { mimeType, direction: options.direction, purpose, messageIndex, partIndex, role,
          ...(toolCallId ? { toolCallId } : {}), capture: {
            ...capture,
            ...(options.direction === "input" && mimeType.startsWith("image/") && options.traceConfig?.hideInputImages ? { captureContent: false } : {}),
          } });
        if (handle.admission === "queued") {
          let positions = spans!.get(spanKey);
          if (!positions) {
            if (spans!.size >= 1024) spans!.delete(spans!.keys().next().value!);
            positions = new Set(); spans!.set(spanKey, positions);
          }
          if (positions.size < MAX_PARTS) positions.add(key);
          return;
        }
        if (handle.admission !== "too_large" && handle.admission !== "source_unavailable") return;
      }
    };
    const images = ["image/png", "image/jpeg", "image/webp"];
    const parts = (content: unknown, messageIndex: number, role: AttachmentOptions["role"]) => {
      list(content).slice(0, MAX_PARTS).forEach((value, partIndex) => {
        const part = record(value);
        if (part.type === "image_url") attach(() => record(part.image_url).url, images, messageIndex, partIndex, "model_input", undefined, false, role);
        else if (part.type === "input_image") attach(() => part.image_url, images, messageIndex, partIndex, "model_input", undefined, false, role);
        else if (part.type === "input_file" || part.type === "file") attach(() => part.type === "file" ? record(part.file).file_data : part.file_data,
          ["application/pdf", "application/octet-stream"], messageIndex, partIndex, "document", undefined, false, role);
      });
    };
    const body = record(payload);
    if (options.direction === "output" && body.error) return;
    if (options.direction === "input") {
      const messages = options.operation === "chat" ? body.messages : body.input;
      list(messages).slice(0, MAX_PARTS).forEach((value, messageIndex) => {
        const message = record(value);
        if (message.type === "computer_call_output") {
          const output = record(message.output);
          if (output.type === "computer_screenshot") attach(() => output.image_url, images, messageIndex, 0, "computer_screenshot",
            typeof message.call_id === "string" ? message.call_id : undefined, false, "tool");
        } else if (!message.type || message.type === "message") {
          const role = ["user", "assistant", "tool", "system"].includes(String(message.role)) ? message.role as AttachmentOptions["role"] : "unknown";
          parts(message.content, messageIndex, role);
        }
      });
    } else if (options.operation === "images") {
      list(body.data).slice(0, MAX_PARTS).forEach((value, index) => {
        const item = record(value);
        // The Images API defaults to PNG. Explicit response format wins where provided.
        const format = body.output_format ?? options.imageFormat;
        const mime = format === "jpeg" ? "image/jpeg" : format === "webp" ? "image/webp" : "image/png";
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
}

/** Telemetry-only typed projection. Never send media bytes, filenames or source URLs to OTLP. */
export function projectOpenAIMedia(payload: unknown, operation: "chat" | "responses" | "images", direction: "input" | "output", redactImages = true): any {
  const body = record(payload);
  const projectPart = (value: unknown): unknown => {
    const part = record(value);
    return (["input_file", "file"].includes(String(part.type)) || redactImages && ["image_url", "input_image"].includes(String(part.type))) ? { type: part.type } : value;
  };
  if (operation === "images") return { created: body.created };
  const field = direction === "input" ? operation === "chat" ? "messages" : "input" : "output";
  if (!Array.isArray(body[field])) return payload;
  return { ...body, [field]: list(body[field]).map(value => {
    const message = record(value);
    if (message.type === "computer_call_output") return { type: message.type, call_id: message.call_id, output: { type: "computer_screenshot" } };
    if (message.type === "image_generation_call") return { type: message.type, id: message.id, status: message.status };
    if (Array.isArray(message.content)) return { ...message, content: message.content.map(projectPart) };
    return value;
  }) };
}
