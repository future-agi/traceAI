import { createFetchHook, FetchHook } from "./wire/fetch-runtime";
import type { SpanResult } from "./wire/core";
export type { SpanResult } from "./wire/core";
export type { FetchHook } from "./wire/fetch-runtime";
let installed: FetchHook | undefined;
/** Explicit fetch-only capture for isolates. No exporter, env, Node APIs or context SDK. */
export function patchFetch(max: number, emit: (result: SpanResult, start: number, end: number) => void): FetchHook {
  if (installed) return installed;
  if (!Number.isFinite(max) || max < 0) throw new Error("traceai edge: max must be a non-negative byte limit");
  const hook = createFetchHook(Math.floor(max), (result, _parent, start, end) => emit(result, start, end), {
    active: () => undefined, skip: () => false, run: (_parent, fn) => fn(),
  });
  const installedHook = { pending: hook.pending, remove() { hook.remove(); if (installed === installedHook) installed = undefined; } };
  installed = installedHook;
  return installed;
}
