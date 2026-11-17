import { defaultTextMapGetter, propagation, ROOT_CONTEXT, trace, type Context } from "@opentelemetry/api";
import { W3CTraceContextPropagator } from "@opentelemetry/core";

/**
 * Incoming HTTP SERVER spans join a remote parent through the application's
 * propagator. When the application registered none, the agent reads the W3C
 * traceparent with this private propagator instead. It is never registered with
 * the API, so the application's own OpenTelemetry setup is not affected.
 */
export const HTTP_SCOPE = "@opentelemetry/instrumentation-http";
const w3c = new W3CTraceContextPropagator();
// The remote parent read for the SERVER span that the HTTP instrumentation starts
// next, synchronously after its start hook. Used once and never kept past this turn.
let pending: Context | undefined;

function applicationPropagator(): boolean {
  try {
    const propagator = (propagation as any)._getGlobalPropagator?.();
    return !propagator || propagator.constructor?.name !== "NoopTextMapPropagator";
  } catch { return true; }
}

/** startIncomingSpanHook for the agent's HTTP instrumentation. It adds no attributes. */
export function readIncomingParent(request: unknown): Record<string, never> {
  pending = undefined;
  try {
    // An application propagator, even one that ignores the header, decides alone.
    if (applicationPropagator()) return {};
    const headers = request && typeof request === "object" ? (request as { headers?: unknown }).headers : undefined;
    if (!headers || typeof headers !== "object") return {};
    const ctx = w3c.extract(ROOT_CONTEXT, headers, defaultTextMapGetter);
    if (!trace.getSpanContext(ctx)) return {};
    pending = ctx;
    queueMicrotask(() => { if (pending === ctx) pending = undefined; });
  } catch { pending = undefined; }
  return {};
}

/** The parent for a SERVER span from the agent's HTTP instrumentation. */
export function incomingParent(ctx: Context | undefined): Context | undefined {
  const parent = pending;
  pending = undefined;
  // Without an application propagator the API's extract returns the root unchanged.
  return parent && (ctx === undefined || ctx === ROOT_CONTEXT) ? parent : ctx;
}
