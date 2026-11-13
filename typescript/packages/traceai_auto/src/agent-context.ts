import { AsyncLocalStorage } from "node:async_hooks";
import { context, ROOT_CONTEXT, trace, type Context, type Span, type SpanOptions, type Tracer, type TracerProvider } from "@opentelemetry/api";

/**
 * The agent never registers an OpenTelemetry context manager, tracer provider
 * or propagator. When the application has a context manager, agent code reads
 * and sets context through the API, so AI spans parent under the application's
 * active span. When it has none, a private AsyncLocalStorage keeps parent links
 * between the spans that agent-bound tracers start. It is never registered with
 * the API, so the application's own OpenTelemetry setup is not affected.
 */
const store = new AsyncLocalStorage<Context>();
// The context each entered span started in, so later work steps out of it once it ends.
const parents = new WeakMap<object, Context>();
// Spans made by agent-bound tracers, including wire spans.
const agentSpans = new WeakSet<object>();

/** True when anyone, normally the application, registered an OpenTelemetry context manager. */
export function applicationContextManager(): boolean {
  try {
    const manager = (context as any)._getContextManager?.();
    return !!manager && manager.constructor?.name !== "NoopContextManager";
  } catch { return false; }
}

function current(ctx: Context): Context {
  // An entered span stays in the private store after it ends. Step out to the
  // context it started in, so later work is not parented under an ended span.
  for (let depth = 0; depth < 128; depth++) {
    const span = trace.getSpan(ctx), parent = span ? parents.get(span) : undefined;
    if (!span || !parent) return ctx;
    let recording = false;
    try { recording = span.isRecording() === true; } catch { /* Treat as ended. */ }
    if (recording) return ctx;
    ctx = parent;
  }
  return ROOT_CONTEXT;
}

/** The context that agent code treats as active. */
export function activeContext(): Context {
  if (applicationContextManager()) return context.active();
  try { return current(store.getStore() ?? ROOT_CONTEXT); } catch { return ROOT_CONTEXT; }
}

/** Run fn with ctx active for agent code, without registering anything globally. */
export function withContext<T>(ctx: Context, fn: () => T): T {
  return applicationContextManager() ? context.with(ctx, fn) : store.run(ctx, fn);
}

export function markAgentSpan(span: object): void { agentSpans.add(span); }
export function isAgentSpan(span: object): boolean { return agentSpans.has(span); }

function enter(parent: Context, span: Span): void {
  try {
    if (!span.isRecording()) return;
    parents.set(span, parent);
    // Instrumentations call the API's context.with, which does nothing without
    // a context manager. Entering here makes this span the parent of the work
    // its caller starts next, until the span ends.
    store.enterWith(trace.setSpan(parent, span));
  } catch { /* Parent links are best effort and never change the application. */ }
}

function startSpan(tracer: Tracer, name: string, options?: SpanOptions, ctx?: Context): Span {
  if (applicationContextManager()) {
    const span = tracer.startSpan(name, options, ctx);
    markAgentSpan(span);
    return span;
  }
  const parent = ctx === undefined || ctx === ROOT_CONTEXT ? activeContext() : ctx;
  const span = tracer.startSpan(name, options, parent);
  markAgentSpan(span);
  enter(parent, span);
  return span;
}

function startActiveSpan(tracer: Tracer, args: unknown[]): unknown {
  const [name, second, third, fourth] = args as [string, unknown, unknown, unknown];
  let options: SpanOptions | undefined, ctx: Context | undefined, fn: (span: Span) => unknown;
  if (typeof second === "function") fn = second as typeof fn;
  else if (typeof third === "function") { options = second as SpanOptions; fn = third as typeof fn; }
  else { options = second as SpanOptions; ctx = third as Context; fn = fourth as typeof fn; }
  if (applicationContextManager()) {
    const parent = ctx ?? context.active(), span = tracer.startSpan(name, options, parent);
    markAgentSpan(span);
    return context.with(trace.setSpan(parent, span), fn, undefined, span);
  }
  const parent = ctx === undefined || ctx === ROOT_CONTEXT ? activeContext() : ctx;
  const span = tracer.startSpan(name, options, parent);
  markAgentSpan(span);
  try { if (span.isRecording()) parents.set(span, parent); } catch { /* Best effort. */ }
  return store.run(trace.setSpan(parent, span), () => fn(span));
}

/**
 * Tracers from this provider start spans on the given provider, mark them as
 * agent spans and keep parent links without a global context manager. SDK
 * pipeline slots stay visible through the prototype; nothing native is patched.
 */
export function contextBoundProvider(provider: TracerProvider): TracerProvider {
  const tracers = new WeakMap<object, Tracer>();
  const facade = Object.create(provider) as TracerProvider;
  Object.defineProperty(facade, "getTracer", { value: (...args: Parameters<TracerProvider["getTracer"]>) => {
    const native = provider.getTracer(...args);
    let bound = tracers.get(native);
    if (!bound) {
      bound = Object.create(native) as Tracer;
      Object.defineProperties(bound, {
        startSpan: { value: (name: string, options?: SpanOptions, ctx?: Context) => startSpan(native, name, options, ctx) },
        startActiveSpan: { value: (...rest: unknown[]) => startActiveSpan(native, rest) },
      });
      tracers.set(native, bound);
    }
    return bound;
  } });
  return facade;
}
