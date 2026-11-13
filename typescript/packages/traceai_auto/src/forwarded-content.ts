import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import type { PrivacyOptions } from "./export-privacy";

// Prompt and response keys that other AI tracers write. FI_HIDE_* hides them on
// application spans that are forwarded to Future AGI.
const INPUT_KEY = /^(?:ai\.prompt(?:\.|$)|ai\.toolCall\.args$|gen_ai\.prompt\.|llm\.prompts(?:\.|$)|traceloop\.entity\.input$)/;
const OUTPUT_KEY = /^(?:ai\.response\.|ai\.toolCall\.result$|gen_ai\.completion\.|traceloop\.entity\.output$)/;
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
    const hidden = hiding.input && INPUT_KEY.test(key) || hiding.output && OUTPUT_KEY.test(key) && !OUTPUT_METADATA.has(key);
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
