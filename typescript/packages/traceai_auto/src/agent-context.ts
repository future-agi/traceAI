import { AsyncLocalStorage } from "node:async_hooks";
import { context, createContextKey, ROOT_CONTEXT, SpanKind, trace, type Context, type Span, type SpanOptions, type Tracer, type TracerProvider } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import { getAttributesFromContext, setMetadata, setSession, setUser } from "@traceai/fi-core";
import { HTTP_SCOPE, incomingParent } from "./incoming-parent";

/**
 * The agent never registers an OpenTelemetry context manager, tracer provider
 * or propagator. When the application has a context manager, agent code reads
 * and sets context through the API, so AI spans parent under the application's
 * active span. When it has none, a private AsyncLocalStorage keeps parent links
 * between the spans that agent-bound tracers start. It is never registered with
 * the API, so the application's own OpenTelemetry setup is not affected.
 *
 * The state is shared by every loaded copy of the agent (the preload bundle and
 * the package an application imports), so the public helpers reach the agent.
 */
const REGISTRY = Symbol.for("traceai.auto.context.v1");
interface ContextRegistry {
  version: 1;
  store: AsyncLocalStorage<Context>;
  // The context each entered span started in, so later work steps out of it once it ends.
  parents: WeakMap<object, Context>;
  // Spans made by agent-bound tracers, including wire spans.
  agentSpans: WeakSet<object>;
}
const registryHost = globalThis as typeof globalThis & { [REGISTRY]?: ContextRegistry };
const sharedRegistry = registryHost[REGISTRY];
const registry: ContextRegistry = sharedRegistry?.version === 1 ? sharedRegistry
  : { version: 1, store: new AsyncLocalStorage<Context>(), parents: new WeakMap<object, Context>(), agentSpans: new WeakSet<object>() };
if (!sharedRegistry) Object.defineProperty(registryHost, REGISTRY, { value: Object.freeze(registry) });
const { store, parents, agentSpans } = registry;

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

// Marks the context of one integration call. Each call gets a new context object:
// AsyncLocalStorage.run, given the store that is already current, may skip the
// restore when fn returns.
const INTEGRATION_CALL = createContextKey("traceai.auto.integration_call");

/**
 * Run one patched SDK call of an integration in its own scope of the private context.
 * Without an application context manager, a span the integration starts in the call is
 * entered only inside this scope. The SDK's own request starts inside it, so it runs
 * under that span, and the wire layer yields to it. When the call returns, the caller's
 * context is back: a request the caller sends beside the call, in the same tick or while
 * it reads a stream, starts outside the span and is captured. With an application
 * context manager, the integration's own context.with already ends with the call.
 */
export function runIntegrationCall<T>(fn: () => T): T {
  if (applicationContextManager()) return fn();
  let scope: Context | undefined;
  try { scope = (store.getStore() ?? ROOT_CONTEXT).setValue(INTEGRATION_CALL, true); } catch { /* The call runs as it is. */ }
  return scope ? store.run(scope, fn) : fn();
}

/**
 * Public helpers. They run fn with a context for AI spans: in the agent's private
 * context, and through the API's context.with, which reaches the application's
 * context manager when it has one. They register nothing globally.
 */
export function runInContext<T>(ctx: Context, fn: () => T): T {
  return store.run(ctx, () => context.with(ctx, fn));
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

// Without an application context manager, FI tracers read the API's empty context.
// Spans parented through the private context also take its context attributes
// (session, user, metadata, tags), unless the caller already set them.
function withContextAttributes(options: SpanOptions | undefined, parent: Context): SpanOptions | undefined {
  try {
    const inherited = getAttributesFromContext(parent);
    if (!Object.keys(inherited).length) return options;
    return { ...options, attributes: { ...inherited, ...options?.attributes } };
  } catch { return options; }
}

export function markAgentSpan(span: object): void { agentSpans.add(span); }
export function isAgentSpan(span: object): boolean { return agentSpans.has(span); }

function enter(parent: Context, span: Span): void {
  try {
    if (!span.isRecording()) return;
    parents.set(span, parent);
    // Instrumentations call the API's context.with, which does nothing without
    // a context manager. Entering here makes this span the parent of the work
    // its caller starts next, until the span ends. Inside runIntegrationCall that
    // is the SDK call's own work only: the scope ends when the call returns.
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
  const span = tracer.startSpan(name, parent !== ctx ? withContextAttributes(options, parent) : options, parent);
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
  const span = tracer.startSpan(name, parent !== ctx ? withContextAttributes(options, parent) : options, parent);
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
