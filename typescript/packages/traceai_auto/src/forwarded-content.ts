import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import type { PrivacyOptions } from "./export-privacy";

// Prompt and response keys that other AI tracers write. FI_HIDE_* hides them on
// application spans that are forwarded to Future AGI.
const INPUT_KEY = /^(?:ai\.prompt(?:\.|$)|ai\.toolCall\.args$|gen_ai\.prompt\.|llm\.prompts(?:\.|$)|traceloop\.entity\.input$|llm\.input_messages(?:\.|$)|embedding\.embeddings\.\d+\.embedding\.text$)/;
const OUTPUT_KEY = /^(?:ai\.response\.|ai\.toolCall\.result$|gen_ai\.completion\.|traceloop\.entity\.output$|llm\.output_messages(?:\.|$))/;
// The Vercel AI SDK content keys that @traceai/vercel hides with the same flags
// (kept in step with its privacy.ts input and output key sets).
const INPUT_KEYS: ReadonlySet<string> = new Set([
  "input.value", "input.mime_type", "raw.input", "ai.prompt", "ai.prompt.messages", "ai.prompt.tools",
  "gen_ai.input.messages", "gen_ai.system_instructions", "gen_ai.tool.call.arguments",
  "gen_ai.tool.definitions", "ai.toolCall.args", "tool.parameters",
  "ai.value", "ai.values", "embedding.text", "ai.documents", "ai.request.text",
  "reranker.query", "reranker.input_documents", "llm.prompt_template.variables",
  "gen_ai.prompt.template.variables", "ai.schema", "ai.schema.description",
  "ai.decision.state", "ai.decision.questions",
]);
const OUTPUT_KEYS: ReadonlySet<string> = new Set([
  "output.value", "output.mime_type", "raw.output", "gen_ai.output.messages",
  "ai.response.text", "ai.response.object", "ai.response.toolCalls",
  "ai.response.reasoning", "ai.toolCall.result", "gen_ai.tool.call.result",
  "message.tool_calls", "reranker.output_documents", "ai.decision.answers", "ai.ranking",
]);
// Provider metadata can echo either side, so either flag hides it.
const EITHER_KEY = "ai.response.providerMetadata";
// Response metadata that is not content.
const OUTPUT_METADATA = new Set(["ai.response.id", "ai.response.model", "ai.response.finishReason", "ai.response.timestamp",
  "ai.response.msToFirstChunk", "ai.response.msToFinish", "ai.response.avgOutputTokensPerSecond", "ai.response.avgCompletionTokensPerSecond"]);
const HIDDEN = "[REDACTED]";

export interface ForwardedHiding { input: boolean; output: boolean }
export function forwardedHiding(options: PrivacyOptions = {}): ForwardedHiding | undefined {
  const input = !!(options.hideInputs || options.hideInputMessages || options.hideInputText);
  const output = !!(options.hideOutputs || options.hideOutputMessages || options.hideOutputText);
  return input || output ? { input, output } : undefined;
}

function hide(attributes: Record<string, unknown> | undefined, hiding: ForwardedHiding): Record<string, unknown> | undefined {
  if (!attributes || typeof attributes !== "object") return undefined;
  let copy: Record<string, unknown> | undefined;
  for (const key of Object.keys(attributes)) {
    const hidden = hiding.input && (INPUT_KEY.test(key) || INPUT_KEYS.has(key))
      || hiding.output && (OUTPUT_KEY.test(key) || OUTPUT_KEYS.has(key)) && !OUTPUT_METADATA.has(key)
      || key === EITHER_KEY;
    if (!hidden) continue;
    copy ??= { ...attributes };
    copy[key] = HIDDEN;
  }
  return copy;
}

/**
 * A view of a forwarded application span with other tracers' prompt and response
 * keys hidden. The application's span is not changed; the view reads every other
 * field, including its span context, from the original.
 */
export function hideForwardedContent(span: ReadableSpan, hiding: ForwardedHiding): ReadableSpan {
  try {
    const attributes = hide(span.attributes as Record<string, unknown>, hiding);
    let events: ReadableSpan["events"] | undefined;
    if (Array.isArray(span.events)) {
      const changed = span.events.map(event => {
        const hidden = hide(event.attributes as Record<string, unknown> | undefined, hiding);
        return hidden ? { ...event, attributes: hidden as typeof event.attributes } : event;
      });
      if (changed.some((event, i) => event !== span.events[i])) events = changed;
    }
    if (!attributes && !events) return span;
    const view = Object.create(span) as ReadableSpan;
    if (attributes) Object.defineProperty(view, "attributes", { value: attributes, enumerable: true });
    if (events) Object.defineProperty(view, "events", { value: events, enumerable: true });
    return view;
  } catch {
    // An unreadable span keeps none of its attributes or events.
    const view = Object.create(span) as ReadableSpan;
    Object.defineProperty(view, "attributes", { value: {}, enumerable: true });
    Object.defineProperty(view, "events", { value: [], enumerable: true });
    return view;
  }
}
