import { context, createContextKey, ROOT_CONTEXT, SpanKind, trace, TraceFlags, type Context, type Span, type SpanOptions, type Tracer, type TracerProvider } from "@opentelemetry/api";
import { isTracingSuppressed, suppressTracing } from "@opentelemetry/core";
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
  // Older copies of the helper consult this cell-local liveness set.
  liveDropped?: WeakSet<Span>;
  // The spans this call was opened for. Any other span started in the call makes nothing active.
  readonly accepts?: AcceptSpan;
}

/** A span start, as an integration call's accept check sees it: the tracer's name and the span kind. */
export interface SpanStart { readonly scope: string | undefined; readonly kind: SpanKind | undefined }
export type AcceptSpan = (start: SpanStart) => boolean;

/** For an incoming request: only the SERVER span the agent's HTTP instrumentation starts. */
export const incomingServerSpan: AcceptSpan = start => start.scope === HTTP_SCOPE && start.kind === SpanKind.SERVER;

function callCell(ctx: Context): CallCell | undefined {
  const cell = ctx.getValue(INTEGRATION_CALL) as CallCell | undefined;
  return cell && cell.active instanceof WeakMap && cell.before instanceof WeakMap && (cell.accepts === undefined || typeof cell.accepts === "function") ? cell : undefined;
}

// A derived context can carry a span directly and a nested call replaces its
// call cell. Remember the owning cell's links by span, across package copies.
interface CallLink { before: CallCell["before"]; droppedEnded?: boolean }
const LINKS = Symbol.for("traceai.auto.call-links.v1");
const host = globalThis as typeof globalThis & { [LINKS]?: WeakMap<Span, CallLink> };
const callLinks = host[LINKS] ?? new WeakMap<Span, CallLink>();
if (!host[LINKS]) Object.defineProperty(host, LINKS, { value: callLinks });

function inCall(ctx: Context): Context {
  try {
    const cell = callCell(ctx), override = cell?.active.get(ctx);
    if (!override) {
      const span = trace.getSpan(ctx);
      // Leave explicitly carried application spans alone. Only a dropped
      // request whose end we actually observed needs its parent replaced.
      if (!span || !callLinks.get(span)?.droppedEnded) return ctx;
    }
    let active = override ?? ctx, derived: Context | undefined, seen: Set<Span> | undefined;
    for (;;) {
      const span = trace.getSpan(active);
      if (!span) return derived ? trace.deleteSpan(derived) : active;
      const link = callLinks.get(span), before = (link?.before ?? cell?.before)?.get(span);
      // No owner means we reached the ambient parent, not another call span.
      if (!before) return derived ? trace.setSpan(derived, span) : active;
      try {
        const live = link?.droppedEnded !== undefined ? !link.droppedEnded : cell?.liveDropped?.has(span) || span.isRecording();
        if (live) {
          return derived ? trace.setSpan(derived, span) : active;
        }
      } catch { /* Treat a failing span as ended and continue unwinding. */ }
      // Keep the session, suppression, and call cell of a context carrying the
      // ended request. Only its span is stale, not the rest of that context.
      if (link?.droppedEnded && active !== override) derived ??= active;
      seen ??= new Set<Span>();
      if (seen.has(span)) return trace.deleteSpan(derived ?? ctx);
      seen.add(span);
      active = before;
    }
  } catch { return ctx; /* Parent links are best effort. */ }
}

// Inside an integration call, a span the call was opened for is active for the rest
// of that call, where the context it started in is current. Any other span, and any
// span outside a call, makes nothing active: the caller's next work never runs under it.
function activateInCall(parent: Context, span: Span, start: SpanStart): void {
  try {
    const current = activeFIContext(), cell = callCell(current);
    if (!cell || (cell.accepts && !cell.accepts(start))) return;
    const link: CallLink = { before: cell.before };
    if (!span.isRecording()) {
      // Only the incoming HTTP request needs a dropped span installed as its
      // parent. A no-op/suppressed span must not displace the current context.
      if (cell.accepts !== incomingServerSpan || isTracingSuppressed(current) || isTracingSuppressed(parent)) return;
      const remote = trace.getSpanContext(parent), local = span.spanContext();
      if (!remote?.isRemote || !trace.isSpanContextValid(remote) || (remote.traceFlags & TraceFlags.SAMPLED) ||
          !trace.isSpanContextValid(local) || local.traceId !== remote.traceId) return;
      // isRecording() cannot distinguish a live dropped span from an ended one.
      // Observe end only on this request's span, not on the tracer or prototype.
      const live = cell.liveDropped ??= new WeakSet<Span>();
      link.droppedEnded = false;
      const end = span.end;
      span.end = function(...args) { link.droppedEnded = true; live.delete(span); return end.apply(this, args); };
      live.add(span);
    }
    const before = inCall(current);
    cell.before.set(span, before);
    callLinks.set(span, link);
    // The remote parent supplied the dropped span's identity, not the session
    // or user values of the private scope in which the request runs.
    cell.active.set(current, trace.setSpan(link.droppedEnded === false ? before : parent, span));
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
 * accepts names the spans the call is opened for; without it, every span started in the call.
 */
export function runIntegrationCall<T>(fn: () => T, accepts?: AcceptSpan): T {
  if (applicationContextManager()) return fn();
  let scope: Context | undefined;
  try {
    const cell: CallCell = { active: new WeakMap<Context, Context>(), before: new WeakMap<object, Context>(), accepts };
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

function startSpan(tracer: Tracer, scope: string | undefined, name: string, options?: SpanOptions, ctx?: Context): Span {
  if (applicationContextManager()) {
    const span = tracer.startSpan(name, options, ctx);
    markAgentSpan(span);
    return span;
  }
  const { parent, implicit } = parentFor(ctx);
  const span = tracer.startSpan(name, implicit ? withContextAttributes(options, parent) : options, parent);
  markAgentSpan(span);
  // Instrumentations call the API's context.with, which does nothing without a
  // context manager. Inside an integration call opened for this span, the call's cell
  // makes it the parent of the rest of the call's own work. Otherwise nothing changes.
  activateInCall(parent, span, { scope, kind: options?.kind });
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
 * pipeline slots stay visible through the prototype; the provider and tracer are not patched.
 */
export function contextBoundProvider(provider: TracerProvider): TracerProvider {
  const tracers = new WeakMap<object, Tracer>();
  const facade = Object.create(provider) as TracerProvider;
  Object.defineProperty(facade, "getTracer", { value: (...args: Parameters<TracerProvider["getTracer"]>) => {
    const native = provider.getTracer(...args);
    const scope = typeof args[0] === "string" ? args[0] : undefined, http = scope === HTTP_SCOPE;
    let bound = tracers.get(native);
    if (!bound) {
      bound = Object.create(native) as Tracer;
      Object.defineProperties(bound, {
        // Incoming SERVER spans join a remote parent read by the private propagator
        // when the application has none.
        startSpan: { value: (name: string, options?: SpanOptions, ctx?: Context) =>
          startSpan(native, scope, name, options, http && options?.kind === SpanKind.SERVER ? incomingParent(ctx) : ctx) },
        startActiveSpan: { value: (...rest: unknown[]) => startActiveSpan(native, rest) },
      });
      tracers.set(native, bound);
    }
    return bound;
  } });
  return facade;
}
