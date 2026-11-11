import type { Span } from "@opentelemetry/api";
import type { TraceConfigOptions } from "@traceai/fi-core";
import type { AttachmentCapturePolicy, AttachmentManager } from "@traceai/fi-core/attachments";

/** One observed message/tool result has a bounded synchronous decode budget. */
const DECODE_BUDGET = 256 * 1024;

export interface AgentAttachmentContext {
  attachments?: AttachmentManager;
  attachmentCapture?: AttachmentCapturePolicy;
  attachmentTraceConfig?: TraceConfigOptions;
}

/**
 * Original Anthropic image/document base64 blocks only, before text truncation.
 * No arbitrary object walk, URL/file-ID resolver, path opening or stream reads.
 * Capturing an existing tool screenshot never starts a recorder.
 */
export function captureAgentMedia(
  config: AgentAttachmentContext,
  span: Span,
  content: unknown,
  options: { toolCallId?: string; messageIndex: number; isError?: boolean; toolResult: boolean },
): void {
  const manager = config.attachments;
  if (!manager || !Array.isArray(content)) return;
  let remaining = DECODE_BUDGET;
  for (let partIndex = 0; partIndex < Math.min(content.length, 64); partIndex++) {
    try {
      const block = content[partIndex];
      if (!block || (block.type !== "image" && block.type !== "document")) continue;
      const source = block.source;
      if (!source || source.type !== "base64" || typeof source.media_type !== "string") continue;
      const mimeType = source.media_type;
      // Do not let a misleading MIME relabel an image to bypass hideInputImages.
      if (block.type === "image" ? !mimeType.startsWith("image/") : mimeType !== "application/pdf") continue;
      const f = config.attachmentTraceConfig ?? {};
      const c = config.attachmentCapture ?? {};
      const errorDetail = options.isError === true || c.errorDetail === true;
      const hidden = f.hideInputs || f.hideInputMessages || f.hideInputText ||
        (block.type === "image" && f.hideInputImages) ||
        (options.toolResult && (f.hideOutputs || f.hideOutputMessages || f.hideOutputText)) ||
        (errorDetail && f.hideErrorDetails) || f.piiRedaction;
      manager.attach(span, {
        kind: "Bytes",
        get data(): Uint8Array {
          // Manager privacy, bootstrap, sampling and descriptor gates precede this getter.
          const encoded = source.data;
          if (typeof encoded !== "string" || !encoded.length || encoded.length > 4 * Math.ceil(remaining / 3)) throw new Error("media unavailable");
          if (encoded.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) throw new Error("media unavailable");
          const size = encoded.length / 4 * 3 - (encoded.endsWith("==") ? 2 : encoded.endsWith("=") ? 1 : 0);
          if (size > remaining) throw new Error("media unavailable");
          remaining -= size;
          return Buffer.from(encoded, "base64");
        },
      }, {
        mimeType,
        direction: options.toolResult ? "output" : "input",
        purpose: options.toolResult ? "tool_result" : "model_input",
        role: options.toolResult ? "tool" : "user",
        toolCallId: options.toolCallId,
        messageIndex: options.messageIndex,
        partIndex,
        capture: { ...c, captureContent: hidden ? false : c.captureContent, modelInput: true, containsOutput: options.toolResult || c.containsOutput === true, errorDetail },
      });
    } catch {
      // Untrusted properties / malformed blocks do not change the SDK iterator.
    }
  }
}
