import { createContextKey, ROOT_CONTEXT, trace, propagation, type Context, type Span } from "@opentelemetry/api";

// Weak identity state, never a process-wide set of trace IDs, URLs or headers.
// It outlives a single request so await continuations under the initiating Context
// cannot restart propagation. Reusing that identity is conservatively closed.
interface Fence { closed: boolean }
const fences = new WeakMap<object, Fence>();
function fence(identity: object): Fence {
  let value = fences.get(identity);
  if (!value) { value = { closed: false }; fences.set(identity, value); }
  return value;
}
const closedKey = createContextKey("traceai outgoing cross-origin redirect");

export function outgoingRedirectClosed(ctx: Context): boolean {
  try {
    const span = trace.getSpan(ctx), baggage = propagation.getBaggage(ctx);
    return ctx.getValue(closedKey) === true || fences.get(ctx)?.closed === true ||
      !!span && fences.get(span)?.closed === true || !!baggage && fences.get(baggage)?.closed === true;
  } catch { return true; }
}
export function closeOutgoingRedirect(ctx: Context): void {
  // Never taint the shared root: unrelated contextless traffic has no chain identity.
  if (ctx === ROOT_CONTEXT) return;
  fence(ctx).closed = true;
  try { const span = trace.getSpan(ctx); if (span) fence(span).closed = true; } catch { /* Context identity remains closed. */ }
  try { const baggage = propagation.getBaggage(ctx); if (baggage) fence(baggage).closed = true; } catch { /* Context identity remains closed. */ }
}
export function outgoingRedirectContext(ctx: Context): Context {
  closeOutgoingRedirect(ctx);
  // Inherit closure across async work started by response listeners, even when
  // the listener replaces its span or baggage. Never change the app's Context.
  return ctx.setValue(closedKey, true);
}
export function noteOutgoingRedirectSpan(span: Span, parent?: Context): void {
  if (!parent) return;
  // Share a mutable fence with observed descendants, including children created
  // BEFORE the redirect response arrives. No parent chains or IDs are retained.
  const identity = trace.getSpan(parent) ?? propagation.getBaggage(parent) ?? (parent === ROOT_CONTEXT ? undefined : parent);
  if (identity) fences.set(span, fence(identity));
  if (outgoingRedirectClosed(parent)) fence(span).closed = true;
}
