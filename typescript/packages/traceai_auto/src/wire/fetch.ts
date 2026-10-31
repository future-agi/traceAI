import { context, createContextKey, trace, Context } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { createFetchHook } from "./fetch-runtime";
import { decodeNodeBody } from "./encoding-node";
import { observePromise, preparePromiseObservation } from "./promise-node";
import type { SpanResult } from "./core";
import { observeOutgoing } from "./observe-node";
export type { FetchHook } from "./fetch-runtime";
const IN_FLIGHT = createContextKey("traceai.auto.wire.in_flight");
const DEDUP_KINDS = new Set(["LLM", "EMBEDDING", "RERANKER", "GUARDRAIL"]);
// Weak ancestry retains neither completed traces nor a process-wide span-id cache.
// Keep live parents, not a cached boolean: instrumentors may set model-kind after onStart.
const ancestors = new WeakMap<object, object>();
export function trackCoverage(span: object, parent: Context): void {
  const active = trace.getSpan(parent);
  if (active) ancestors.set(span, active);
}
export function shouldSkip(ctx: Context): boolean {
  if (isTracingSuppressed(ctx) || ctx.getValue(IN_FLIGHT)) return true;
  let span: any = trace.getSpan(ctx);
  const seen = new Set<object>();
  for (let depth = 0; span && depth < 256 && !seen.has(span); depth++, span = span.span ?? ancestors.get(span)) {
    seen.add(span);
    if (DEDUP_KINDS.has(span.attributes?.["gen_ai.span.kind"] ?? span.attributes?.["fi.span.kind"])) return true;
  }
  return false;
}
export function patchFetch(max: number, emit: (result: SpanResult, parent: Context, start: number, end: number) => void, secrets: string[] = []) {
  preparePromiseObservation();
  return createFetchHook(max, emit, { active: () => context.active(), skip: shouldSkip, observe: observePromise, observeRequest: observeOutgoing,
    run: (parent, fn) => context.with(parent.setValue(IN_FLIGHT, true), fn) }, secrets, decodeNodeBody);
}
