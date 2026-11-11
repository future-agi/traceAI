import type { Span } from "@opentelemetry/api";
import type { TraceConfigOptions } from "@traceai/fi-core";
import type { AttachmentCapturePolicy, AttachmentManager } from "@traceai/fi-core/attachments";

/** One observed message/tool result has a bounded synchronous decode budget. */
const DECODE_BUDGET = 256 * 1024;

/** Decoded length of canonical padded base64 within `limit`, without decoding; 0 when invalid or too large. */
function decodedSize(encoded: unknown, limit: number): number {
  if (typeof encoded !== "string" || !encoded.length || encoded.length > 4 * Math.ceil(limit / 3)) return 0;
  if (encoded.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) return 0;
  const size = encoded.length / 4 * 3 - (encoded.endsWith("==") ? 2 : encoded.endsWith("=") ? 1 : 0);
  return size > 0 && size <= limit ? size : 0;
}
function decodeBase64(encoded: unknown, limit: number): Uint8Array | undefined {
  const size = decodedSize(encoded, limit);
  if (!size) return undefined;
  const bytes = Buffer.from(encoded as string, "base64");
  return bytes.byteLength === size ? bytes : undefined;
}

export interface AgentAttachmentContext {
  attachments?: AttachmentManager;
  attachmentCapture?: AttachmentCapturePolicy;
  attachmentTraceConfig?: TraceConfigOptions;
}

/**
 * Original Anthropic image/document base64 blocks only, before text truncation.
 * No arbitrary object walk, URL/file-ID resolver, path opening or stream reads.
 * Capturing an existing tool screenshot never starts a recorder.
 * Every block is preflighted (deny-wins, budget reserved) before its base64 value is read or decoded; an admitted
 * block whose bytes turn out malformed or over budget is released. SDK frames are complete parsed messages, never
 * consumed byte streams, so consumed-stream capture does not apply.
 */
export function captureAgentMedia(
  config: AgentAttachmentContext,
  span: Span,
  content: unknown,
  options: { toolCallId?: string; messageIndex: number; isError?: boolean; toolResult: boolean },
  observed?: WeakSet<object>,
): void {
  const manager = config.attachments;
  // A manager without preflight (an older core) cannot gate before decode: capture nothing.
  if (!manager || typeof manager.preflight !== "function" || !Array.isArray(content)) return;
  let remaining = DECODE_BUDGET;
  for (let partIndex = 0; partIndex < Math.min(content.length, 64) && remaining > 0; partIndex++) {
    try {
      const block = content[partIndex];
      if (!block || (block.type !== "image" && block.type !== "document")) continue;
      const source = block.source;
      if (!source || source.type !== "base64" || typeof source.media_type !== "string") continue;
      const mimeType = source.media_type;
      // Do not let a misleading MIME relabel an image to bypass hideInputImages.
      if (block.type === "image" ? !mimeType.startsWith("image/") : mimeType !== "application/pdf") continue;
      // One occurrence per original block object per query: a replayed/re-yielded frame is not new media.
      // Identity only; the encoded data is never read for deduplication.
      if (observed) { if (observed.has(source)) continue; observed.add(source); }
      const f = config.attachmentTraceConfig ?? {};
      const c = config.attachmentCapture ?? {};
      const errorDetail = options.isError === true || c.errorDetail === true;
      const hidden = f.hideInputs || f.hideInputMessages || f.hideInputText ||
        (block.type === "image" && f.hideInputImages) ||
        (options.toolResult && (f.hideOutputs || f.hideOutputMessages || f.hideOutputText)) ||
        (errorDetail && f.hideErrorDetails) || f.piiRedaction;
      const request = {
        mimeType,
        direction: (options.toolResult ? "output" : "input") as "output" | "input",
        purpose: (options.toolResult ? "tool_result" : "model_input") as "tool_result" | "model_input",
        capture: { ...c, captureContent: hidden ? false : c.captureContent, modelInput: true, containsOutput: options.toolResult || c.containsOutput === true, errorDetail },
      };
      // Preflight (deny-wins, budget reserved) BEFORE the base64 value is read or decoded (design section 1).
      // The value is read at most once, and only after a preflight passed every privacy, correlation and sampling step.
      let encoded: unknown, read = false;
      const value = () => { if (!read) { read = true; encoded = source.data; } return encoded; };
      let ticket = manager.preflight(span, { ...request, maxBytes: remaining });
      // too_large (negotiated limit below the budget) and queue_full (owned budget nearly used) come after every
      // privacy, correlation and sampling step, so sizing the block exactly and retrying once is safe.
      if (ticket.reason === "too_large" || ticket.reason === "queue_full") {
        const exact = decodedSize(value(), remaining);
        if (exact > 0) ticket = manager.preflight(span, { ...request, maxBytes: exact });
      }
      if (!ticket.admitted) continue;
      const bytes = decodeBase64(value(), Math.min(ticket.maxBytes, remaining));
      // Admitted but malformed or over budget: the reservation is returned and nothing is captured.
      if (!bytes) { ticket.release(); continue; }
      remaining -= bytes.byteLength;
      ticket.attach({ kind: "Bytes", data: bytes }, {
        role: options.toolResult ? "tool" : "user",
        toolCallId: options.toolCallId,
        messageIndex: options.messageIndex,
        partIndex,
      });
    } catch {
      // Untrusted properties / malformed blocks do not change the SDK iterator.
    }
  }
}
