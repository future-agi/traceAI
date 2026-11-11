import { context, trace } from "@opentelemetry/api";
import { captureMediaMessages, captureMediaParts, mediaField, type FrameworkAttachmentOptions } from "@traceai/framework-attachments";

/**
 * Wrap the implementation passed to Genkit defineModel, not an exported span.
 * Genkit owns the active action span; this adapter never creates a second span.
 * Pass the provider-owned manager, and the action's content restrictions.
 * No inference or upload is awaited beyond the original implementation promise.
 */
export function withGenkitModelAttachments<Args extends unknown[], Result>(
  binding: FrameworkAttachmentOptions, implementation: (...args: Args) => Promise<Result>,
): (...args: Args) => Promise<Result> {
  return async function (this: unknown, ...args: Args): Promise<Result> {
    const span = trace.getSpan(context.active());
    if (span?.isRecording()) captureMediaMessages(binding, span, mediaField(args[0], "messages"), "genkit", "input");
    const result = await implementation.apply(this, args);
    if (span?.isRecording()) {
      // ModelResponseData's message is original typed content. No JSON recovery.
      captureMediaMessages(binding, span, [mediaField(result, "message")], "genkit", "output");
    }
    return result;
  };
}

/**
 * Only tools explicitly returning Genkit Part[] are media boundaries. Ordinary
 * tool JSON is not searched for URLs or keys that happen to look like media.
 * Tool output is also model input, so input and output restrictions intersect.
 */
export function withGenkitToolAttachments<Args extends unknown[], Result>(
  binding: FrameworkAttachmentOptions, implementation: (...args: Args) => Promise<Result>,
): (...args: Args) => Promise<Result> {
  return async function (this: unknown, ...args: Args): Promise<Result> {
    const span = trace.getSpan(context.active());
    const result = await implementation.apply(this, args);
    if (span?.isRecording()) captureMediaParts(binding, span, result, "genkit", {
      direction: "output", purpose: "tool_result", messageIndex: 0, role: "tool", capture: { modelInput: true },
    });
    return result;
  };
}
export type { FrameworkAttachmentOptions } from "@traceai/framework-attachments";
