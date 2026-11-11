import type { TraceConfigOptions } from "@traceai/fi-core";
import type { AttachmentManager, AttachmentSpan, AttachmentCorrelation, AttachmentCapturePolicy, AttachmentOptions } from "@traceai/fi-core/attachments";

/** Native SDK span ownership is deliberately not overridden by this adapter. */
export const anthropicAttachmentCapabilities = Object.freeze({
  schemaVersion: 1,
  operations: ["messages.create.input", "tool_result.input"] as const,
  sources: ["inline_base64"] as const,
  unsupported: ["remote_url", "provider_file", "native_span_hookup", "generated_image", "beta_messages_hookup"] as const,
  maxInlineBytes: 256 * 1024,
});
type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue => value !== null && typeof value === "object" ? value as RecordValue : {};
const registryKey = Symbol.for("@traceai/anthropic/attachment-observations/v1");
type Registry = WeakMap<AttachmentManager, Map<string, Set<string>>>;
const host = globalThis as typeof globalThis & { [registryKey]?: Registry };
const observations = (host[registryKey] ??= new WeakMap());

/** Observe only Messages' typed image/document and one-level tool_result blocks.
 * The manager owns policy, upload, lifecycle and references. A native integration
 * must supply its actual emitted span/correlation, never an ambient guessed span.
 */
export function captureAnthropicAttachments(manager: AttachmentManager | undefined,
  span: AttachmentSpan | AttachmentCorrelation, payload: unknown,
  options: { traceConfig?: TraceConfigOptions; capture?: AttachmentCapturePolicy } = {}): void {
  if (!manager) return;
  try {
    const correlation = "snapshot" in span ? span : manager.correlation(span);
    const identity = correlation.snapshot();
    if (!identity?.sampled) return;
    const messages = record(payload).messages;
    if (!Array.isArray(messages)) return;
    let spans = observations.get(manager);
    if (!spans) { spans = new Map(); observations.set(manager, spans); }
    const spanKey = `${identity.traceId}/${identity.spanId}`;
    const config = options.traceConfig ?? {};
    const hidden = config.hideInputs || config.hideInputMessages || config.hideInputText || config.piiRedaction;
    const computerNames = new Set<string>();
    const tools = record(payload).tools;
    if (Array.isArray(tools)) for (const value of tools.slice(0, 256)) {
      const tool = record(value);
      if (typeof tool.type === "string" && /^computer_\d{8}$/.test(tool.type) && typeof tool.name === "string") computerNames.add(tool.name);
    }
    const computerCalls = new Set<string>();
    for (const value of messages.slice(0, 256)) {
      const message = record(value);
      if (message.role !== "assistant" || !Array.isArray(message.content)) continue;
      for (const value of message.content.slice(0, 256)) {
        const block = record(value);
        if (block.type === "tool_use" && typeof block.id === "string" && typeof block.name === "string" && computerNames.has(block.name)) computerCalls.add(block.id);
      }
    }
    let visited = 0;
    messages.slice(0, 256).forEach((value, messageIndex) => {
      const message = record(value);
      if (!Array.isArray(message.content)) return;
      // Nested positions are relative to their tool result, identified by toolCallId.
      const visit = (value: unknown, index: number, position: string, toolCallId?: string, errorDetail = false) => {
        if (++visited > 256) return;
        const part = record(value);
        if (part.type !== "image" && part.type !== "document") return;
        const source = record(part.source);
        if (source.type !== "base64") return;
        const mime = source.media_type;
        if (typeof mime !== "string" || !(part.type === "image" ? ["image/png", "image/jpeg", "image/webp"] : ["application/pdf"]).includes(mime)) return;
        const key = `input/${messageIndex}/${position}`;
        if (spans!.get(spanKey)?.has(key)) return;
        const role: AttachmentOptions["role"] = toolCallId ? "tool" : message.role === "assistant" ? "assistant" : "user";
        const handle = manager.attach(correlation, { kind: "Bytes", get data() {
          const encoded = source.data;
          if (typeof encoded !== "string" || !encoded.length || encoded.length % 4 !== 0 ||
            encoded.length > 4 * Math.ceil(anthropicAttachmentCapabilities.maxInlineBytes / 3) || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) return new Uint8Array();
          const bytes = Buffer.from(encoded, "base64");
          return bytes.length <= anthropicAttachmentCapabilities.maxInlineBytes && bytes.toString("base64") === encoded ? bytes : new Uint8Array();
        } }, { mimeType: mime, direction: "input", messageIndex, partIndex: index, role,
          purpose: part.type === "document" ? "document" : toolCallId ? computerCalls.has(toolCallId) ? "computer_screenshot" : "tool_result" : "model_input",
          ...(toolCallId ? { toolCallId } : {}),
          capture: { ...options.capture, modelInput: true, errorDetail: errorDetail || options.capture?.errorDetail,
            ...(hidden || (part.type === "image" && config.hideInputImages) || (errorDetail && config.hideErrorDetails) ? { captureContent: false } : {}) },
        });
        if (handle.admission === "queued") {
          let positions = spans!.get(spanKey);
          if (!positions) {
            if (spans!.size >= 1024) spans!.delete(spans!.keys().next().value!);
            positions = new Set(); spans!.set(spanKey, positions);
          }
          if (positions.size < 256) positions.add(key);
        }
      };
      message.content.slice(0, 256).forEach((value, index) => {
        const block = record(value);
        if (block.type === "tool_result" && Array.isArray(block.content)) {
          // Invalid tool IDs fail closed instead of losing causal identity.
          if (typeof block.tool_use_id !== "string" || !/^[a-zA-Z0-9_-]{1,128}$/.test(block.tool_use_id)) return;
          block.content.slice(0, 256).forEach((child, childIndex) => visit(child, childIndex, `${index}/${childIndex}`, block.tool_use_id as string, block.is_error === true));
        } else visit(value, index, String(index));
      });
    });
  } catch { /* No source-bearing exceptions or changes to the application's result. */ }
}

/** A telemetry-only projection; never mutates the request passed to the SDK. */
export function projectAnthropicMedia(payload: unknown): any {
  const body = record(payload);
  if (!Array.isArray(body.messages)) return payload;
  const project = (value: unknown, nested = false): unknown => {
    const block = record(value);
    if (block.type === "image" || block.type === "document") return { type: block.type };
    if (block.type === "tool_result" && Array.isArray(block.content)) return {
      type: block.type, tool_use_id: block.tool_use_id, is_error: block.is_error,
      content: nested ? [] : block.content.map(child => project(child, true)),
    };
    return block;
  };
  return { ...body, messages: body.messages.map(value => {
    const message = record(value);
    return Array.isArray(message.content) ? { ...message, content: message.content.map(value => project(value)) } : message;
  }) };
}
