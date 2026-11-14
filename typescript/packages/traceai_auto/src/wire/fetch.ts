import { createContextKey, trace, Context } from "@opentelemetry/api";
import { autoModelResponsibility, autoModelWireAllowed, hasAutoModelCoverage } from "@traceai/fi-core";
import { isTracingSuppressed } from "@opentelemetry/core";
import { createFetchHook } from "./fetch-runtime";
import { decodeNodeBody } from "./encoding-node";
import { observePromise, preparePromiseObservation } from "./promise-node";
import type { SpanResult } from "./core";
import { observeOutgoing } from "./observe-node";
import { activeContext, isAgentSpan, withContext } from "../agent-context";
export type { FetchHook } from "./fetch-runtime";
const IN_FLIGHT = createContextKey("traceai.auto.wire.in_flight");
const DEDUP_KINDS = new Set(["LLM", "EMBEDDING", "RERANKER", "GUARDRAIL"]);
// Compatibility only for unrelated, unscoped instrumentations. Auto-owned scopes
// carry their own flat Context authority and never depend on observer callbacks.
const ancestors = new WeakMap<object, object>();
// The same tracked spans while they are live, by trace and span id. On the AI-only
// route the agent never sees the application's spans, so an application span made
// inside an AI call has no ancestry entry. Its own parent id still names the agent's
// span, so the walk steps over that one application span.
const live = new Map<string, object>();
const LIVE_LIMIT = 4096;
function own(value: any, key: string): any {
  const descriptor = Object.getOwnPropertyDescriptor(value, key);
  return descriptor && "value" in descriptor ? descriptor.value : undefined;
}
function liveKey(span: any): string | undefined {
  try {
    const { traceId, spanId } = span.spanContext();
    return typeof traceId === "string" && typeof spanId === "string" ? `${traceId}-${spanId}` : undefined;
  } catch { return undefined; }
}
function liveParent(span: any): object | undefined {
  try {
    // SDK 2.x keeps the parent's span context; 1.x keeps only its span id.
    const spanId = own(span, "parentSpanContext")?.spanId ?? own(span, "parentSpanId");
    const traceId = span.spanContext().traceId;
    return typeof spanId === "string" && typeof traceId === "string" ? live.get(`${traceId}-${spanId}`) : undefined;
  } catch { return undefined; }
}
export function trackCoverage(span: object, parent: Context): void {
  if (autoModelResponsibility(parent)) return;
  const active = trace.getSpan(parent);
  if (active) ancestors.set(span, active);
  const key = liveKey(span);
  if (!key) return;
  live.set(key, span);
  if (live.size > LIVE_LIMIT) live.delete(live.keys().next().value as string);
}
/** Called when a tracked span ends. */
export function endCoverage(span: object): void {
  const key = liveKey(span);
  if (key && live.get(key) === span) live.delete(key);
}
// Which model spans wire capture yields to. On the AI-only route only spans the
// agent made reach Future AGI, so a model span on the application's own provider
// does not hide the call. Elsewhere every model span shares the destination.
let yieldsTo: (span: any) => boolean = () => true;
function wireAllowed(ctx: Context, phase: "guard" | "inspect" | "request"): boolean {
  const responsibility = autoModelResponsibility(ctx);
  if (responsibility) return autoModelWireAllowed(responsibility.operation, responsibility.independentCoverage, phase);
  // Only genuine absence enters the compatibility path. An explicit null TOOL
  // scope must never reacquire a MODEL above its boundary via this legacy cache.
  let span: any = trace.getSpan(ctx);
  const seen = new Set<object>();
  for (let depth = 0; span && depth < 256 && !seen.has(span); depth++, span = span.span ?? ancestors.get(span) ?? liveParent(span)) {
    seen.add(span);
    const kind = span.attributes?.["gen_ai.span.kind"] ?? span.attributes?.["fi.span.kind"];
    if (kind !== undefined && kind !== null && !DEDUP_KINDS.has(kind)) break;
    if ((hasAutoModelCoverage(span) || DEDUP_KINDS.has(kind)) && yieldsTo(span)) return false;
  }
  return true;
}
export function shouldSkip(ctx: Context, consultOperation = true): boolean {
  return isTracingSuppressed(ctx) || !!ctx.getValue(IN_FLIGHT) || !wireAllowed(ctx, consultOperation ? "inspect" : "guard");
}
/** onCapture runs when a supported AI request is captured, with its parent context.
 * agentSpansOnly: yield only to model spans made by agent-bound tracers. */
export function patchFetch(max: number, emit: (result: SpanResult, parent: Context, start: number, end: number) => void, secrets: string[] = [], onCapture?: (parent: Context) => void, agentSpansOnly = false) {
  yieldsTo = agentSpansOnly ? span => isAgentSpan(span) || (!!span?.span && typeof span.span === "object" && isAgentSpan(span.span)) : () => true;
  preparePromiseObservation();
  // The application's context manager when it has one, otherwise the agent's private context.
  return createFetchHook(max, emit, { active: () => activeContext(), skip: parent => shouldSkip(parent, false),
    // Same context decision, now after supported URL matching. Only the emit
    // completion boundary can commit wire; credential observation stays separate.
    capture: parent => {
      const allowed = wireAllowed(parent, "request");
      if (allowed) { try { onCapture?.(parent); } catch { /* Bookkeeping never changes capture. */ } }
      return allowed;
    }, observe: observePromise, observeRequest: observeOutgoing,
    run: (parent, fn) => withContext(parent.setValue(IN_FLIGHT, true), fn) }, secrets, decodeNodeBody);
}
