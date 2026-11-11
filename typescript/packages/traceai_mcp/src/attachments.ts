import type { TraceConfigOptions } from "@traceai/fi-core";
import type { Span, SpanContext } from "@opentelemetry/api";
import type { AttachmentCapturePolicy, AttachmentHandle, AttachmentManager } from "@traceai/fi-core/attachments";

/** Bounded synchronous decode budget, independent of the manager's owned queue. */
export const MCP_MEDIA_DECODE_BUDGET = 256 * 1024;
const MAX_PARTS = 64;
/** A denied ticket returns its denial handle without reading this. */
const NO_SOURCE = Object.freeze({ kind: "Bytes" as const, data: new Uint8Array(0) });

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

/**
 * Observe only MCP CallToolResult.content / ReadResourceResult.contents.
 * No structuredContent scraping, URI retrieval, path access or stream reads.
 * Tool results are also model inputs: both directional policies apply.
 * Every media part is preflighted (deny-wins, budget reserved) before its encoded value is read or decoded.
 * Returns one handle per media part that reached preflight: denied parts carry their denial reason; an admitted part
 * whose bytes turn out malformed or over the decode budget is released and omitted.
 * Results are fully parsed objects, never consumed byte streams, so consumed-stream capture does not apply.
 */
export function captureMCPResult(
  manager: AttachmentManager,
  span: Span | SpanContext,
  result: unknown,
  operation: "tools/call" | "resources/read" = "tools/call",
  capture: AttachmentCapturePolicy = {},
  traceConfig: TraceConfigOptions = {},
): AttachmentHandle[] {
  const handles: AttachmentHandle[] = [];
  let remaining = MCP_MEDIA_DECODE_BUDGET;
  try {
    // A manager without preflight (an older core) cannot gate before decode: capture nothing.
    if (typeof manager?.preflight !== "function") return handles;
    if (!result || typeof result !== "object") return handles;
    const envelope = result as Record<string, unknown>;
    const parts = operation === "tools/call" ? envelope.content : envelope.contents;
    if (!Array.isArray(parts)) return handles;
    for (let index = 0; index < Math.min(parts.length, MAX_PARTS) && remaining > 0; index++) {
      try {
        const part = parts[index];
        if (!part || typeof part !== "object") continue;
        let media: Record<string, unknown>;
        let field: "data" | "blob";
        if (operation === "resources/read") {
          media = part;
          field = "blob";
        } else if (part.type === "image" || part.type === "audio") {
          media = part;
          field = "data";
        } else if (part.type === "resource" && part.resource && typeof part.resource === "object") {
          media = part.resource;
          field = "blob";
        } else {
          continue;
        }
        const mimeType = media.mimeType;
        if (typeof mimeType !== "string") continue;
        if (operation === "tools/call" && (part.type === "image" || part.type === "audio") && !mimeType.startsWith(`${part.type}/`)) continue;
        const errorDetail = capture.errorDetail === true || envelope.isError === true;
        const hidden = traceConfig.hideInputs || traceConfig.hideInputMessages || traceConfig.hideInputText ||
          traceConfig.hideOutputs || traceConfig.hideOutputMessages || traceConfig.hideOutputText ||
          (mimeType.startsWith("image/") && traceConfig.hideInputImages) ||
          (errorDetail && traceConfig.hideErrorDetails) || traceConfig.piiRedaction;
        const request = {
          mimeType,
          direction: "output" as const,
          purpose: "tool_result" as const,
          capture: { ...capture, captureContent: hidden ? false : capture.captureContent, modelInput: true, containsOutput: true, errorDetail },
        };
        // Preflight (deny-wins, budget reserved) BEFORE the encoded value is read or decoded (design section 1).
        // The value is read at most once, and only after a preflight passed every privacy, correlation and sampling step.
        let encoded: unknown, read = false;
        const source = () => { if (!read) { read = true; encoded = media[field]; } return encoded; };
        let ticket = manager.preflight(span, { ...request, maxBytes: remaining });
        // too_large (negotiated limit below the budget) and queue_full (owned budget nearly used) come after every
        // privacy, correlation and sampling step, so sizing the part exactly and retrying once is safe.
        if (ticket.reason === "too_large" || ticket.reason === "queue_full") {
          const exact = decodedSize(source(), remaining);
          if (exact > 0) ticket = manager.preflight(span, { ...request, maxBytes: exact });
        }
        if (!ticket.admitted) { handles.push(ticket.attach(NO_SOURCE)); continue; }
        const bytes = decodeBase64(source(), Math.min(ticket.maxBytes, remaining));
        // Admitted but malformed or over budget: the reservation is returned and nothing is captured.
        if (!bytes) { ticket.release(); continue; }
        remaining -= bytes.byteLength;
        const handle = ticket.attach({ kind: "Bytes", data: bytes }, { role: "tool", partIndex: index });
        handles.push(handle);
      } catch {
        // Malformed/untrusted media must not change the tool response or diagnostics.
      }
    }
  } catch {
    // Content-free failure; never stringify a provider response or exception.
  }
  return handles;
}
