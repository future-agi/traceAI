import type { TraceConfigOptions } from "@traceai/fi-core";
import type { AttachmentManager, AttachmentSpan, AttachmentCorrelation, AttachmentCapturePolicy, AttachmentOptions } from "@traceai/fi-core/attachments";

/** Native SDK span ownership is deliberately not overridden by this adapter. */
export const anthropicAttachmentCapabilities = Object.freeze({
  schemaVersion: 1,
  operations: ["messages.create.input", "tool_result.input"] as const,
  sources: ["inline_base64"] as const,
  /** Fail closed: no capture is attempted for these. */
  unsupported: ["remote_url", "provider_file", "native_span_hookup", "generated_image", "beta_messages_hookup", "streamed_output"] as const,
  maxInlineBytes: 256 * 1024,
});

/** Admitted media positions for one observation: position key -> occurrence. */
export type AnthropicCapturedMedia = ReadonlyMap<string, Readonly<{ occurrenceId: string; mimeType: string }>>;
type Captured = Map<string, Readonly<{ occurrenceId: string; mimeType: string }>>;
type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue => value !== null && typeof value === "object" ? value as RecordValue : {};
const MAX_PARTS = 256;
const MAX_SPANS = 1024;
const EMPTY: AnthropicCapturedMedia = new Map();
const registryKey = Symbol.for("@traceai/anthropic/attachment-observations/v1");
type Registry = WeakMap<AttachmentManager, Map<string, Captured>>;
const host = globalThis as typeof globalThis & { [registryKey]?: Registry };
const observations = (host[registryKey] ??= new WeakMap());
const TOOL_ID = /^[a-zA-Z0-9_-]{1,128}$/;

const MAX_ENCODED = 4 * Math.ceil(anthropicAttachmentCapabilities.maxInlineBytes / 3);

/** Exact decoded length from the encoded string's shape only (no decode); 0 when out of bounds. */
function bound(encoded: unknown): number {
  if (typeof encoded !== "string" || !encoded.length || encoded.length % 4 !== 0 || encoded.length > MAX_ENCODED) return 0;
  return (encoded.length / 4) * 3 - (encoded.endsWith("==") ? 2 : encoded.endsWith("=") ? 1 : 0);
}

/** Strict decode after admission. The ticket's budget is a hard bound; padding and alphabet are strict. */
function decode(encoded: unknown, maxBytes: number): Uint8Array | undefined {
  // Bound allocation BEFORE decoding.
  if (!bound(encoded) || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded as string)) return undefined;
  const bytes = Buffer.from(encoded as string, "base64");
  return bytes.length && bytes.length <= maxBytes && bytes.length <= anthropicAttachmentCapabilities.maxInlineBytes &&
    bytes.toString("base64") === encoded ? bytes : undefined;
}

/** Observe only Messages' typed image/document blocks and one level of tool_result content.
 * Every part is preflighted (deny-wins, budget reserved) before its base64 is read or decoded; an admitted
 * ticket that is not used is released at once. The manager owns policy, upload, lifecycle and references. A native integration
 * must supply its actual emitted span/correlation, never an ambient guessed span.
 * Returns the admitted positions so the caller can project them out of span telemetry.
 */
export function captureAnthropicAttachments(manager: AttachmentManager | undefined,
  span: AttachmentSpan | AttachmentCorrelation, payload: unknown,
  options: { traceConfig?: TraceConfigOptions; capture?: AttachmentCapturePolicy } = {}): AnthropicCapturedMedia {
  if (!manager) return EMPTY;
  const captured: Captured = new Map();
  try {
    const correlation = "snapshot" in span ? span : manager.correlation(span);
    const identity = correlation.snapshot();
    if (!identity?.sampled) return EMPTY;
    const messages = record(payload).messages;
    if (!Array.isArray(messages)) return EMPTY;
    let spans = observations.get(manager);
    if (!spans) { spans = new Map(); observations.set(manager, spans); }
    const spanKey = `${identity.traceId}/${identity.spanId}`;
    const config = options.traceConfig ?? {};
    const hidden = config.hideInputs || config.hideInputMessages || config.hideInputText || config.piiRedaction;
    // Screenshot classification requires a typed computer tool AND an assistant tool_use naming it.
    const computerNames = new Set<string>();
    const tools = record(payload).tools;
    if (Array.isArray(tools)) for (const value of tools.slice(0, MAX_PARTS)) {
      const tool = record(value);
      if (typeof tool.type === "string" && /^computer_\d{8}$/.test(tool.type) && typeof tool.name === "string") computerNames.add(tool.name);
    }
    const computerCalls = new Set<string>();
    for (const value of messages.slice(0, MAX_PARTS)) {
      const message = record(value);
      if (message.role !== "assistant" || !Array.isArray(message.content)) continue;
      for (const value of message.content.slice(0, MAX_PARTS)) {
        const block = record(value);
        if (block.type === "tool_use" && typeof block.id === "string" && typeof block.name === "string" && computerNames.has(block.name)) computerCalls.add(block.id);
      }
    }
    let visited = 0;
    messages.slice(0, MAX_PARTS).forEach((value, messageIndex) => {
      const message = record(value);
      if (!Array.isArray(message.content)) return;
      // Nested positions are relative to their tool result, identified by toolCallId.
      const visit = (value: unknown, index: number, position: string, toolCallId?: string, errorDetail = false) => {
        if (++visited > MAX_PARTS) return;
        const part = record(value);
        if (part.type !== "image" && part.type !== "document") return;
        const source = record(part.source);
        if (source.type !== "base64") return;
        const mime = source.media_type;
        if (typeof mime !== "string" || !(part.type === "image" ? ["image/png", "image/jpeg", "image/webp"] : ["application/pdf"]).includes(mime)) return;
        const positionKey = `input/${messageIndex}/${position}`;
        const previous = spans!.get(spanKey)?.get(positionKey);
        if (previous) { captured.set(positionKey, previous); return; }
        const role: AttachmentOptions["role"] = toolCallId ? "tool" : message.role === "assistant" ? "assistant" : "user";
        const request = { mimeType: mime, direction: "input" as const,
          purpose: (part.type === "document" ? "document" : toolCallId ? computerCalls.has(toolCallId) ? "computer_screenshot" : "tool_result" : "model_input") as AttachmentOptions["purpose"],
          capture: { ...options.capture, modelInput: true, errorDetail: errorDetail || options.capture?.errorDetail,
            ...(hidden || (part.type === "image" && config.hideInputImages) || (errorDetail && config.hideErrorDetails) ? { captureContent: false } : {}) } };
        // Preflight (deny-wins, budget reserved) before the base64 source is read or decoded (design section 1).
        // The source is read at most once.
        let data: unknown, dataRead = false;
        const read = () => { if (!dataRead) { dataRead = true; data = source.data; } return data; };
        let ticket = manager.preflight(correlation, { ...request, maxBytes: anthropicAttachmentCapabilities.maxInlineBytes });
        // too_large (negotiated limit below the cap) and queue_full (owned budget nearly used) are evaluated after every
        // privacy, correlation and sampling step, so sizing the source exactly and retrying once is safe here.
        if (ticket.reason === "too_large" || ticket.reason === "queue_full") {
          const exact = bound(read());
          if (exact > 0) ticket = manager.preflight(correlation, { ...request, maxBytes: exact });
        }
        if (!ticket.admitted) return;
        const bytes = decode(read(), ticket.maxBytes);
        if (!bytes) { ticket.release(); return; }
        const handle = ticket.attach({ kind: "Bytes", data: bytes }, { messageIndex, partIndex: index, role, ...(toolCallId ? { toolCallId } : {}) });
        if (handle.admission === "queued") {
          const occurrence = Object.freeze({ occurrenceId: handle.occurrenceId, mimeType: mime });
          let positions = spans!.get(spanKey);
          if (!positions) {
            if (spans!.size >= MAX_SPANS) spans!.delete(spans!.keys().next().value!);
            positions = new Map(); spans!.set(spanKey, positions);
          }
          if (positions.size < MAX_PARTS) positions.set(positionKey, occurrence);
          captured.set(positionKey, occurrence);
        }
      };
      message.content.slice(0, MAX_PARTS).forEach((value, index) => {
        const block = record(value);
        if (block.type === "tool_result" && Array.isArray(block.content)) {
          // Invalid tool IDs fail closed instead of losing causal identity.
          if (typeof block.tool_use_id !== "string" || !TOOL_ID.test(block.tool_use_id)) return;
          block.content.slice(0, MAX_PARTS).forEach((child, childIndex) => visit(child, childIndex, `${index}/${childIndex}`, block.tool_use_id as string, block.is_error === true));
        } else visit(value, index, String(index));
      });
    });
  } catch { /* No source-bearing exceptions or changes to the application's result. */ }
  return captured;
}

/** Telemetry-only projection. ONLY admitted positions are replaced with a `fi-attachment:` occurrence URI
 * source, so uploaded bytes are not exported twice. Everything else is returned exactly as before, and the
 * request passed to the SDK is never mutated.
 */
export function projectAnthropicMedia(payload: unknown, captured: AnthropicCapturedMedia): any {
  if (!captured.size) return payload;
  try {
    const body = record(payload);
    if (!Array.isArray(body.messages)) return payload;
    const replace = (value: unknown, position: string): unknown => {
      const occurrence = captured.get(position);
      return occurrence ? { ...record(value), source: { type: "url", url: `fi-attachment:${occurrence.occurrenceId}` } } : value;
    };
    return { ...body, messages: body.messages.map((value, messageIndex) => {
      const message = record(value);
      if (!Array.isArray(message.content)) return value;
      return { ...message, content: message.content.map((value, index) => {
        const block = record(value);
        if (block.type === "tool_result" && Array.isArray(block.content)) {
          return { ...block, content: block.content.map((child, childIndex) => replace(child, `input/${messageIndex}/${index}/${childIndex}`)) };
        }
        return replace(value, `input/${messageIndex}/${index}`);
      }) };
    }) };
  } catch { return payload; }
}
