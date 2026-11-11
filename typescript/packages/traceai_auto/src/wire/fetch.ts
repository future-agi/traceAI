import { context, createContextKey, trace, Context, type Span } from "@opentelemetry/api";
import { autoModelResponsibility, autoModelWireAllowed, hasAutoModelCoverage } from "@traceai/fi-core";
import { isTracingSuppressed } from "@opentelemetry/core";
import { createFetchHook } from "./fetch-runtime";
import type { AttachmentManager } from "@traceai/fi-core/attachments";
import { wireAttachmentObserver } from "./attachments";
import { decodeNodeBody } from "./encoding-node";
import { observePromise, preparePromiseObservation } from "./promise-node";
import type { SpanResult } from "./core";
import { observeOutgoing } from "./observe-node";
export type { FetchHook } from "./fetch-runtime";
const IN_FLIGHT = createContextKey("traceai.auto.wire.in_flight");
const DEDUP_KINDS = new Set(["LLM", "EMBEDDING", "RERANKER", "GUARDRAIL"]);
// Compatibility only for unrelated, unscoped instrumentations. Auto-owned scopes
// carry their own flat Context authority and never depend on observer callbacks.
const ancestors = new WeakMap<object, object>();
export function trackCoverage(span: object, parent: Context): void {
  if (autoModelResponsibility(parent)) return;
  const active = trace.getSpan(parent);
  if (active) ancestors.set(span, active);
}
function wireAllowed(ctx: Context, phase: "guard" | "inspect" | "request"): boolean {
  const responsibility = autoModelResponsibility(ctx);
  if (responsibility) return autoModelWireAllowed(responsibility.operation, responsibility.independentCoverage, phase);
  // Only genuine absence enters the compatibility path. An explicit null TOOL
  // scope must never reacquire a MODEL above its boundary via this legacy cache.
  let span: any = trace.getSpan(ctx);
  const seen = new Set<object>();
  for (let depth = 0; span && depth < 256 && !seen.has(span); depth++, span = span.span ?? ancestors.get(span)) {
    seen.add(span);
    const kind = span.attributes?.["gen_ai.span.kind"] ?? span.attributes?.["fi.span.kind"];
    if (kind !== undefined && kind !== null && !DEDUP_KINDS.has(kind)) break;
    if (hasAutoModelCoverage(span) || DEDUP_KINDS.has(kind)) return false;
  }
  return true;
}
export function shouldSkip(ctx: Context, consultOperation = true): boolean {
  return isTracingSuppressed(ctx) || !!ctx.getValue(IN_FLIGHT) || !wireAllowed(ctx, consultOperation ? "inspect" : "guard");
}
export function patchFetch(max: number, emit: (result: SpanResult, parent: Context, start: number, end: number, bindMedia?: (span: Span) => void) => void, secrets: string[] = [], attachments?: AttachmentManager) {
  preparePromiseObservation();
  return createFetchHook(max, emit, { active: () => context.active(), skip: parent => attachments ? isTracingSuppressed(parent) || !!parent.getValue(IN_FLIGHT) : shouldSkip(parent, false),
    media: attachments ? (parent, method, url) => wireAttachmentObserver(attachments, parent, method, url) : undefined,
    // Same context decision, now after supported URL matching. Only the emit
    // completion boundary can commit wire; credential observation stays separate.
    capture: parent => wireAllowed(parent, "request"), observe: observePromise, observeRequest: observeOutgoing,
    run: (parent, fn) => context.with(parent.setValue(IN_FLIGHT, true), fn) }, secrets, decodeNodeBody);
}
