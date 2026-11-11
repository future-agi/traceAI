import type { TraceConfigOptions } from "@traceai/fi-core";
import type { Span, SpanContext } from "@opentelemetry/api";
import type { AttachmentCapturePolicy, AttachmentHandle, AttachmentManager } from "@traceai/fi-core/attachments";

/** Bounded synchronous decode budget, independent of the manager's owned queue. */
export const MCP_MEDIA_DECODE_BUDGET = 256 * 1024;
const MAX_PARTS = 64;

/**
 * Observe only MCP CallToolResult.content / ReadResourceResult.contents.
 * No structuredContent scraping, URI retrieval, path access or stream reads.
 * Tool results are also model inputs: both directional policies apply.
 * The manager reads the lazy Bytes source only AFTER its privacy/sampling gate.
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
    if (!result || typeof result !== "object") return handles;
    const envelope = result as Record<string, unknown>;
    const parts = operation === "tools/call" ? envelope.content : envelope.contents;
    if (!Array.isArray(parts)) return handles;
    for (let index = 0; index < Math.min(parts.length, MAX_PARTS); index++) {
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
        const handle = manager.attach(span, {
          kind: "Bytes",
          get data(): Uint8Array {
            // No data getter, length scan or decode before manager admission.
            const encoded = media[field];
            if (typeof encoded !== "string" || !encoded.length || encoded.length > 4 * Math.ceil(remaining / 3)) {
              throw new Error("media unavailable");
            }
            if (encoded.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) throw new Error("media unavailable");
            const size = encoded.length / 4 * 3 - (encoded.endsWith("==") ? 2 : encoded.endsWith("=") ? 1 : 0);
            if (size > remaining) throw new Error("media unavailable");
            remaining -= size;
            return Buffer.from(encoded, "base64");
          },
        }, {
          mimeType,
          direction: "output",
          purpose: "tool_result",
          role: "tool",
          partIndex: index,
          capture: { ...capture, captureContent: hidden ? false : capture.captureContent, modelInput: true, containsOutput: true, errorDetail },
        });
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
