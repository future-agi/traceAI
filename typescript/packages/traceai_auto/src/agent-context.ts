import { context, createContextKey, ROOT_CONTEXT, SpanKind, trace, type Context, type Span, type SpanOptions, type Tracer, type TracerProvider } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { activeFIContext, getAttributesFromContext, runInFIContext, setMetadata, setSession, setUser } from "@traceai/fi-core";
import { HTTP_SCOPE, incomingParent } from "./incoming-parent";

/**
 * The agent never registers an OpenTelemetry context manager, tracer provider
 * or propagator. When the application has a context manager, agent code reads
 * and sets context through the API, so AI spans parent under the application's
 * active span. When it has none, agent code reads and sets context in fi-core's
 * private context (activeFIContext and runInFIContext), the same one that
 * FITracer.startActiveSpan uses. A context set there is active for a callback
 * and the work it starts, and never for the caller's flow after it returns. It
 * is never registered with the API, so the application's own OpenTelemetry
 * setup is not affected.
 *
 * The span marks are shared by every loaded copy of the agent (the preload
 * bundle and the package an application imports), so the public helpers reach the agent.
 */
const REGISTRY = Symbol.for("traceai.auto.context.v2");
interface ContextRegistry {
  version: 2;
  // Spans made by agent-bound tracers, including wire spans.
  agentSpans: WeakSet<object>;
}
const registryHost = globalThis as typeof globalThis & { [REGISTRY]?: ContextRegistry };
const sharedRegistry = registryHost[REGISTRY];
const registry: ContextRegistry = sharedRegistry?.version === 2 ? sharedRegistry : { version: 2, agentSpans: new WeakSet<object>() };
if (!sharedRegistry) Object.defineProperty(registryHost, REGISTRY, { value: Object.freeze(registry) });
const { agentSpans } = registry;

/** True when anyone, normally the application, registered an OpenTelemetry context manager. */
export function applicationContextManager(): boolean {
  try {
    const manager = (context as any)._getContextManager?.();
    return !!manager && manager.constructor?.name !== "NoopContextManager";
  } catch { return false; }
}

// The context of one integration call holds that call's cell under this key. Each
// call gets a new cell and a new context object, so nothing in it reaches the caller
// once the call returns, or a flow started beside the call.
const INTEGRATION_CALL = createContextKey("traceai.auto.integration_call");
interface CallCell {
  // For a context that is current inside the call: that context with the span the
  // integration last started there. Keys are compared by identity, so a context set
  // later inside the call (with runInFIContext or startActiveSpan) is not a key, and
  // the innermost context wins.
  readonly active: WeakMap<Context, Context>;
  // For each span in the cell: what was active where it started, to go back to once it ends.
  readonly before: WeakMap<object, Context>;
}

function callCell(ctx: Context): CallCell | undefined {
  const cell = ctx.getValue(INTEGRATION_CALL) as CallCell | undefined;
  return cell && cell.active instanceof WeakMap && cell.before instanceof WeakMap ? cell : undefined;
}

// The context that is active where ctx is current: inside an integration call, the
// span the integration started there, while it still records; otherwise ctx itself.
function inCall(ctx: Context): Context {
  try {
    const cell = callCell(ctx);
    if (!cell) return ctx;
    let active = cell.active.get(ctx);
    for (let depth = 0; active && active !== ctx && depth < 128; depth++) {
      const span = trace.getSpan(active);
      let recording = false;
      try { recording = span?.isRecording() === true; } catch { /* Treat as ended. */ }
      if (recording) return active;
      // The span has ended: go back to what was active where it started.
      active = span ? cell.before.get(span) : undefined;
    }
  } catch { /* Parent links are best effort. */ }
  return ctx;
}

// Inside an integration call, a span an agent-bound tracer starts is active for the
// rest of that call, where the context it started in is current. Outside a call
// nothing is made active: the caller's next work never runs under the span.
function activateInCall(parent: Context, span: Span): void {
  try {
    if (!span.isRecording()) return;
    const current = activeFIContext(), cell = callCell(current);
    if (!cell) return;
    cell.before.set(span, cell.active.get(current) ?? current);
    cell.active.set(current, trace.setSpan(parent, span));
  } catch { /* Parent links are best effort and never change the application. */ }
}

/** The context that agent code treats as active. */
export function activeContext(): Context {
  if (applicationContextManager()) return context.active();
  try { return inCall(activeFIContext()); } catch { return ROOT_CONTEXT; }
}

/** Run fn with ctx active for agent code, without registering anything globally. */
export function withContext<T>(ctx: Context, fn: () => T): T {
  return applicationContextManager() ? context.with(ctx, fn) : runInFIContext(ctx, fn);
}

/**
 * Run one patched SDK call of an integration in its own scope of the private context.
 * Without an application context manager, a span the integration starts in the call
 * is active for the rest of this call only, through the call's cell. The SDK's own
 * request starts inside the call, so it runs under that span, and the wire layer
 * yields to it. Spans started in the call nest under it. When the call returns, the
 * caller's context is back: a request the caller sends beside the call, in the same
 * tick or while it reads a stream, starts outside the span and is captured. With an
 * application context manager, the integration's own context.with already ends with the call.
 */
export function runIntegrationCall<T>(fn: () => T): T {
  if (applicationContextManager()) return fn();
  let scope: Context | undefined;
  try {
    const cell: CallCell = { active: new WeakMap<Context, Context>(), before: new WeakMap<object, Context>() };
    // A call made inside another call starts under that call's span.
    scope = activeContext().setValue(INTEGRATION_CALL, cell);
  } catch { /* The call runs as it is. */ }
  return scope ? runInFIContext(scope, fn) : fn();
}

/**
 * Public helpers. They run fn with a context for AI spans: in fi-core's private
 * context, and through the API's context.with, which reaches the application's
 * context manager when it has one. They register nothing globally.
 */
export function runInContext<T>(ctx: Context, fn: () => T): T {
  return runInFIContext(ctx, fn);
}
/** AI spans started inside fn carry this session id. Helpers nest. */
export function runWithSession<T>(sessionId: string, fn: () => T): T {
  return runInContext(setSession(activeContext(), { sessionId }), fn);
}
/** AI spans started inside fn carry this user id. */
export function runWithUser<T>(userId: string, fn: () => T): T {
  return runInContext(setUser(activeContext(), { userId }), fn);
}
/** AI spans started inside fn carry this metadata. */
export function runWithMetadata<T>(metadata: Record<string, unknown>, fn: () => T): T {
  return runInContext(setMetadata(activeContext(), metadata), fn);
}
/** No AI span is started inside fn, and OpenTelemetry instrumentations see the suppression too. */
export function runWithTracingSuppressed<T>(fn: () => T): T {
  return runInContext(suppressTracing(activeContext()), fn);
}

// Without an application context manager, tracers that are not FI tracers read the
// API's empty context. Spans parented through the private context also take its
// context attributes (session, user, metadata, tags), unless the caller already set them.
function withContextAttributes(options: SpanOptions | undefined, parent: Context): SpanOptions | undefined {
  try {
    const inherited = getAttributesFromContext(parent);
    if (!Object.keys(inherited).length) return options;
    return { ...options, attributes: { ...inherited, ...options?.attributes } };
  } catch { return options; }
}

export function markAgentSpan(span: object): void { agentSpans.add(span); }
export function isAgentSpan(span: object): boolean { return agentSpans.has(span); }

// Without an application context manager: tracers that pass no context, or the API's
// root context, get the agent's active context. FI tracers pass fi-core's active
// context, which inside an integration call is resolved through the call's cell.
function parentFor(ctx: Context | undefined): { parent: Context; implicit: boolean } {
  if (ctx === undefined || ctx === ROOT_CONTEXT) return { parent: activeContext(), implicit: true };
  return { parent: inCall(ctx), implicit: false };
}

function startSpan(tracer: Tracer, name: string, options?: SpanOptions, ctx?: Context): Span {
  if (applicationContextManager()) {
    const span = tracer.startSpan(name, options, ctx);
    markAgentSpan(span);
    return span;
  }
  const { parent, implicit } = parentFor(ctx);
  const span = tracer.startSpan(name, implicit ? withContextAttributes(options, parent) : options, parent);
  markAgentSpan(span);
  // Instrumentations call the API's context.with, which does nothing without a
  // context manager. Inside an integration call, the call's cell makes this span
  // the parent of the rest of the call's own work. Outside one, nothing changes.
  activateInCall(parent, span);
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
  const { parent, implicit } = parentFor(ctx);
  const span = tracer.startSpan(name, implicit ? withContextAttributes(options, parent) : options, parent);
  markAgentSpan(span);
  // Active for fn and the work it starts, never for the caller after fn returns.
  return runInFIContext(trace.setSpan(parent, span), () => fn(span));
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
    const http = args[0] === HTTP_SCOPE;
    let bound = tracers.get(native);
    if (!bound) {
      bound = Object.create(native) as Tracer;
      Object.defineProperties(bound, {
        // Incoming SERVER spans join a remote parent read by the private propagator
        // when the application has none.
        startSpan: { value: (name: string, options?: SpanOptions, ctx?: Context) =>
          startSpan(native, name, options, http && options?.kind === SpanKind.SERVER ? incomingParent(ctx) : ctx) },
        startActiveSpan: { value: (...rest: unknown[]) => startActiveSpan(native, rest) },
      });
      tracers.set(native, bound);
    }
    return bound;
  } });
  return facade;
}
