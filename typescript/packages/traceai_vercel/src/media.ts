import type { Span } from "@opentelemetry/api";
import { captureMediaMessages, captureMediaParts, mediaField, type FrameworkAttachmentOptions } from "@traceai/framework-attachments";

export function captureVercelInput(binding: FrameworkAttachmentOptions, span: Span, event: unknown): void {
  const messages = mediaField(event, "promptMessages") ?? mediaField(event, "messages") ?? mediaField(event, "prompt");
  captureMediaMessages(binding, span, messages, "vercel", "input");
}
export function captureVercelOutput(binding: FrameworkAttachmentOptions, span: Span, event: unknown): void {
  captureMediaParts(binding, span, mediaField(event, "content"), "vercel", {
    direction: "output", purpose: "model_output", messageIndex: 0, role: "assistant",
  });
}
export function captureVercelToolOutput(binding: FrameworkAttachmentOptions, span: Span, output: unknown): void {
  // AI SDK's explicit content output, not arbitrary tool result objects. A tool
  // image is also model input, so BOTH recordOutputs and input privacy apply.
  if (mediaField(output, "type") !== "content") return;
  captureMediaParts(binding, span, mediaField(output, "value"), "vercel", {
    direction: "output", purpose: "tool_result", messageIndex: 0, role: "tool", capture: { modelInput: true },
  });
}
