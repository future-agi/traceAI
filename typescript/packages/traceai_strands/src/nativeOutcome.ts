import { SpanStatusCode } from "@opentelemetry/api";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";

const FINISH_REASONS = new Set([
  "cancelled", "checkpoint", "contentFiltered", "endTurn", "end_turn", "guardrailIntervened", "interrupt",
  "maxTokens", "limitOutputTokens", "limitStructuredOutputAttempts", "limitTotalTokens", "limitTurns",
  "pauseTurn", "refusal", "stopSequence", "toolUse", "modelContextWindowExceeded",
]);
const NON_SUCCESS = new Set([
  "cancelled", "contentFiltered", "guardrailIntervened", "interrupt", "maxTokens", "limitOutputTokens",
  "limitStructuredOutputAttempts", "limitTotalTokens", "limitTurns", "refusal", "modelContextWindowExceeded",
]);

/** Read completion metadata before dropping content events, without exporting arbitrary payloads. */
export function finishReasons(span: ReadableSpan): string[] {
  const reasons = new Set<string>();
  const add = (value: unknown) => {
    if (typeof value === "string" && FINISH_REASONS.has(value)) reasons.add(value);
  };
  const messages = (value: unknown) => {
    if (typeof value !== "string" || value.length > 1_048_576) return;
    try {
      const parsed: unknown = JSON.parse(value);
      if (Array.isArray(parsed)) for (const message of parsed) add(message?.finish_reason);
    } catch { /* Missing or truncated telemetry is not evidence of success. */ }
  };
  const canonical = span.attributes["gen_ai.response.finish_reasons"];
  if (Array.isArray(canonical)) canonical.forEach(add);
  messages(span.attributes["gen_ai.output.messages"]);
  for (const event of span.events) {
    // These SDK failures can occur before a response/choice event is recorded.
    if (event.name === "exception") {
      if (event.attributes?.["exception.type"] === "CancelledError") add("cancelled");
      if (event.attributes?.["exception.type"] === "MaxTokensError") add("maxTokens");
    }
    if (event.name === "gen_ai.choice") add(event.attributes?.finish_reason);
    if (event.name === "gen_ai.client.inference.operation.details") messages(event.attributes?.["gen_ai.output.messages"]);
  }
  return [...reasons];
}

export function outcome(span: ReadableSpan, reasons: string[]): SpanStatusCode {
  if (span.status.code === SpanStatusCode.ERROR || span.attributes["gen_ai.tool.status"] === "error" ||
      reasons.some((reason) => NON_SUCCESS.has(reason))) return SpanStatusCode.ERROR;
  const operation = span.attributes["gen_ai.operation.name"];
  // The SDK sets OK in finally, even when a consumer returns its iterator early.
  // No completion record means unknown, not success (nor a fabricated failure).
  if ((operation === "chat" || operation === "invoke_agent") && reasons.length === 0) return SpanStatusCode.UNSET;
  return span.status.code;
}

/** Correlate model/tool outcomes to their native cycle across processor batches.
 * No payloads or trace objects are retained; sampling/drops are bounded and fail
 * back to UNSET. Agent roots use their own completion, allowing recovered tools.
 */
export class CycleOutcomes {
  private readonly pending = new Map<string, SpanStatusCode>();

  observe(span: ReadableSpan, code: SpanStatusCode): void {
    const operation = span.attributes["gen_ai.operation.name"];
    if ((operation !== "chat" && operation !== "execute_tool") || !span.parentSpanContext) return;
    const key = `${span.spanContext().traceId}:${span.parentSpanContext.spanId}`;
    const previous = this.pending.get(key);
    const merged = previous === SpanStatusCode.ERROR || code === SpanStatusCode.ERROR ? SpanStatusCode.ERROR :
      previous === SpanStatusCode.UNSET || code === SpanStatusCode.UNSET ? SpanStatusCode.UNSET : code;
    this.pending.set(key, merged);
    if (this.pending.size > 4096) this.pending.delete(this.pending.keys().next().value!);
  }

  take(span: ReadableSpan): SpanStatusCode {
    const key = `${span.spanContext().traceId}:${span.spanContext().spanId}`;
    const code = this.pending.get(key) ?? SpanStatusCode.UNSET;
    this.pending.delete(key);
    return span.status.code === SpanStatusCode.ERROR ? SpanStatusCode.ERROR : code;
  }

  clear(): void { this.pending.clear(); }
}
