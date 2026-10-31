import { Span } from "@opentelemetry/api";
import {
  ContentBlock,
  ConversationRole,
  ConverseResponse,
  ConverseStreamOutput,
} from "@aws-sdk/client-bedrock-runtime";
import { StreamAccumulator } from "../stream";
import { extractConverseResponseAttributes } from "./converse-response-attributes";

/** Reconstruct the same response shape used by non-streaming Converse capture. */
export function createConverseStreamAccumulator(span: Span): StreamAccumulator {
  const blocks = new Map<number, { text?: string; tool?: { id: string; name: string; input: string } }>();
  let role: ConversationRole = "assistant";
  let usage: ConverseResponse["usage"];
  let metrics: ConverseResponse["metrics"];
  let stopReason: ConverseResponse["stopReason"];
  return {
    add(value) {
      const event = value as ConverseStreamOutput;
      if (event.messageStart?.role) role = event.messageStart.role;
      const start = event.contentBlockStart;
      if (start?.start?.toolUse && start.contentBlockIndex !== undefined) {
        const tool = start.start.toolUse;
        blocks.set(start.contentBlockIndex, { tool: { id: tool.toolUseId ?? "", name: tool.name ?? "", input: "" } });
      }
      const delta = event.contentBlockDelta;
      if (delta?.delta && delta.contentBlockIndex !== undefined) {
        const block = blocks.get(delta.contentBlockIndex) ?? {};
        if (delta.delta.text !== undefined) block.text = (block.text ?? "") + delta.delta.text;
        if (delta.delta.toolUse && block.tool) block.tool.input += delta.delta.toolUse.input ?? "";
        blocks.set(delta.contentBlockIndex, block);
      }
      if (event.metadata) {
        usage = event.metadata.usage;
        metrics = event.metadata.metrics;
      }
      if (event.messageStop) stopReason = event.messageStop.stopReason;
    },
    finish() {
      const content: ContentBlock[] = [];
      for (const [, block] of [...blocks].sort(([a], [b]) => a - b)) {
        if (block.text !== undefined) content.push({ text: block.text });
        if (block.tool) {
          let input: NonNullable<ContentBlock["toolUse"]>["input"] = block.tool.input;
          try { input = JSON.parse(input || "{}"); } catch { /* Partial tool JSON on cancellation. */ }
          content.push({ toolUse: { toolUseId: block.tool.id, name: block.tool.name, input } });
        }
      }
      extractConverseResponseAttributes({ span, response: {
        output: { message: { role, content } }, usage, metrics, stopReason,
      } });
    },
  };
}
