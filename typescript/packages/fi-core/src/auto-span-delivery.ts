import { trace, type Context } from "@opentelemetry/api";

/** @internal Auto-owned framework work, not an application observability bus. */
export interface AutoSpanDelivery {
  /** An alternate destination uses the SAME admission phase and deadline. */
  isReady(destination?: () => boolean): boolean;
  canForward(destination?: () => boolean): boolean;
  /** Register synchronously BEFORE invoking upstream formatting/async entry. */
  admit(work: () => Promise<void>): Promise<void>;
  drain(): Promise<void>;
  stopAdmissions(deadline: number): void;
  revoke(): void;
}

export function createAutoSpanDelivery(destinationReady: () => boolean): AutoSpanDelivery {
  let phase: "open" | "draining" | "revoked" = "open";
  let deadline = Infinity;
  let pending = 0;
  const waiters = new Set<() => void>();
  const releaseWaiters = () => {
    for (const resolve of waiters) resolve();
    waiters.clear();
  };
  const canForward = (destination = destinationReady) => {
    if (phase === "revoked" || Date.now() >= deadline) return false;
    try { return destination(); } catch { return false; }
  };
  const isReady = (destination = destinationReady) => phase === "open" && pending < 4096 && canForward(destination);
  return {
    isReady, canForward,
    admit(work) {
      if (!isReady() || pending >= 4096) return Promise.resolve();
      pending++;
      const finish = () => { if (--pending === 0) releaseWaiters(); };
      try {
        const result = work();
        // Return the ORIGINAL promise; neither observation branch can reject.
        void result.then(finish, finish);
        return result;
      } catch (error) { finish(); throw error; }
    },
    drain() {
      return pending === 0 || phase === "revoked" ? Promise.resolve()
        : new Promise<void>(resolve => { waiters.add(resolve); });
    },
    stopAdmissions(until) {
      if (phase === "revoked") return;
      phase = "draining";
      deadline = Math.min(deadline, until);
    },
    revoke() { phase = "revoked"; releaseWaiters(); },
  };
}

/** Responsibility decision/census table (ordered; no collector acknowledgements).
 *
 * Scope authority (before route/census):
 * - MODEL entry starts a fresh operation frame; TOOL/non-model entry shadows it
 *   with an explicit boundary, never with ordinary missing-scope undefined.
 * - Selecting an independent model retains that one candidate; any depth of
 *   neutral Context descendants inherits it, before OR after observer removal.
 * - Selecting an explicit non-model kind clears both candidate and inherited
 *   operation. A later model INSIDE that boundary can own its own request.
 * - Creation/selection uses Context semantics; availability is read at the request
 *   boundary. No observer membership or stale boolean can change scope meaning.
 *
 * Current request / operation       Actual conversion target       Decision
 * Closer recording native model    any                            native REQUEST;
 *   + live SDK sink                                                leave outer route alone
 * Inherited operation=native       any                            native OPERATION,
 *                                                                 even through neutral child
 * Operation=converted (any stage)   live/lost/recovered             no wire retry of conversion
 * Operation=wire                   live/lost/recovered             wire retries; never convert
 * Undecided synthetic operation    attached, live supplied/host    select converted on supported request
 * Undecided synthetic operation    empty Multi/no-op/stopped       wire eligible to independent host sink
 * Undecided synthetic operation    observer detached/no sinks      wire eligible, not yet committed
 * Undecided synthetic operation    opaque custom onEnd             explicit host responsibility contract
 * No operation                    legacy native/coverage contract preserve existing request coverage
 *
 * Target census follows ONLY our registered wrappers to the exact target, then
 * live Multi members. Observer-only wrappers, excluded members, cycles, empty
 * lists, NoopSpanProcessor and shutdown Simple/Batch processors are not sinks.
 * A non-throwing opaque custom onEnd is a local handoff contract, NOT evidence of
 * collection. Membership of our wrapper alone never makes its target ready.
 *
 * The guard phase inspects closer native responsibility before the inherited
 * operation; request phase selects conversion only after a supported URL match.
 * Completion re-reads the SAME retained request Context before any wire claim:
 * a closer native owner recovered by then wins only that request, without claiming
 * its enclosing aggregate. Completion does not select a new converted route;
 * supported-request selection and converted delivery retain their existing seams.
 * Wire commits only at completed capture with a live wire sink. Loss/recovery
 * cannot rewrite a committed operation or earlier capture; a later independent
 * native request may still win while the outer operation remains on its wire route.
 * A closer child does NOT claim its enclosing aggregate: recovery can legitimately
 * deliver that distinct aggregate. No application span attributes are rewritten.
 * Admission, forwarding and wire share the existing drain/deadline/revoke gate.
 */
const SINK_TARGETS = Symbol.for("traceai.auto.span-sink-targets.v1");
const sinkGlobals = globalThis as any;
const sinkTargets: WeakMap<object, object | null> = sinkGlobals[SINK_TARGETS] ?? (sinkGlobals[SINK_TARGETS] = new WeakMap());
/** @internal Register only an auto-owned wrapper; undefined denotes an observer. */
export function rememberAutoSpanSink(wrapper: object, target?: object): void { sinkTargets.set(wrapper, target ?? null); }

/** Inspect responsibility, not remote export success. Unknown custom processors
 * remain application-owned contracts; their onEnd return is not a receipt. */
export function hasAutoSpanSink(pipeline: any, excluded: readonly object[] = []): boolean {
  const seen = new Set<object>();
  const visit = (item: any, depth: number): boolean => {
    if (!item || depth > 16 || seen.has(item) || excluded.includes(item)) return false;
    seen.add(item);
    if (item._shutdownOnce?.isCalled === true || item.constructor?.name === "NoopSpanProcessor") return false;
    if (sinkTargets.has(item)) return visit(sinkTargets.get(item), depth + 1);
    if (Array.isArray(item._spanProcessors)) return item._spanProcessors.some((child: any) => visit(child, depth + 1));
    return typeof item.onEnd === "function";
  };
  try { return visit(pipeline, 0); } catch { return false; }
}

/** @internal Apply the request rows of the census without claiming an aggregate
 * on behalf of an independently instrumented child. Guard/completion are read-only;
 * completion eligibility is followed by claimWire's live destination check. */
export function autoModelWireAllowed(operation: AutoModelOperation | undefined, independentCoverage: boolean,
  phase: "guard" | "inspect" | "request" | "complete"): boolean {
  if (independentCoverage) return false;
  if (!operation || phase === "guard") return true;
  if (phase === "complete") return operation.route !== "native" && operation.route !== "converted";
  return phase === "request" ? operation.considerWire() : !operation.isCovered();
}

type ModelRoute =
  | { kind: "undecided" }
  | { kind: "native" }
  | { kind: "converted"; stage: "selected" | "converting" | "submitted" | "lost" }
  | { kind: "wire"; submitted: number; lost: number };

/** @internal One record per live model operation, shared by context and snapshots.
 * A wire route permits separate retry requests; it never permits a converted copy.
 * Submitted means handed to the selected SDK pipeline, NOT collector success. */
export interface AutoModelOperation {
  readonly route: ModelRoute["kind"];
  snapshot(): Readonly<ModelRoute>;
  isCovered(): boolean;
  selectNative(ready: () => boolean): void;
  /** Called only for a supported request. False commits coverage to conversion. */
  considerWire(): boolean;
  /** Commit wire only when a real observation is built and a sink can accept it. */
  claimWire(canDeliver: () => boolean): boolean;
  finishWire(submitted: boolean): void;
  claimConverted(): boolean;
  finishConverted(submitted: boolean): void;
}
export function createAutoModelOperation(delivery?: AutoSpanDelivery): AutoModelOperation {
  let route: ModelRoute = { kind: "undecided" };
  const ready = () => delivery?.isReady() === true;
  return {
    get route() { return route.kind; },
    snapshot() { return { ...route }; },
    isCovered() { return route.kind === "native" || route.kind === "converted" || (route.kind === "undecided" && ready()); },
    selectNative(nativeReady) {
      if (route.kind !== "undecided") return;
      try { if (nativeReady()) route = { kind: "native" }; } catch { /* No invented coverage. */ }
    },
    considerWire() {
      if (route.kind === "native" || route.kind === "converted") return false;
      if (route.kind === "undecided" && ready()) {
        route = { kind: "converted", stage: "selected" };
        return false;
      }
      // Merely seeing a URL or unread/frozen response does not hand off ownership.
      return true;
    },
    claimWire(canDeliver) {
      if (route.kind === "native" || route.kind === "converted") return false;
      try { if (!canDeliver()) return false; } catch { return false; }
      if (route.kind === "undecided") route = { kind: "wire", submitted: 0, lost: 0 };
      return true;
    },
    finishWire(submitted) {
      if (route.kind === "wire") {
        const key = submitted ? "submitted" : "lost";
        route[key] = Math.min(Number.MAX_SAFE_INTEGER, route[key] + 1);
      }
    },
    claimConverted() {
      if (route.kind === "wire" || route.kind === "native") return false;
      if (route.kind === "converted" && route.stage !== "selected") return false;
      if (delivery && !delivery.canForward()) return false;
      route = { kind: "converted", stage: "converting" };
      return true;
    },
    finishConverted(submitted) {
      if (route.kind === "converted" && route.stage === "converting") {
        route = { kind: "converted", stage: submitted ? "submitted" : "lost" };
      }
      // A failed irreversible route stays failed; recovery is for NEW operations.
    },
  };
}

// Side metadata only, shared by native CJS/ESM copies. No IDs, application object
// writes, strong live-span registry or completed-operation tombstones are needed.
const COVERAGE = Symbol.for("traceai.auto.model-coverage.v2");
const OPERATION = Symbol.for("traceai.auto.model-operation-context.v1");
const globals = globalThis as any;
const coverage: WeakMap<object, AutoModelOperation | (() => boolean)> = globals[COVERAGE] ?? (globals[COVERAGE] = new WeakMap());
export function rememberAutoModelOperation(span: object, operation: AutoModelOperation): void { coverage.set(span, operation); }
export function getAutoModelOperation(span: object): AutoModelOperation | undefined {
  const value = coverage.get(span);
  return typeof value === "object" ? value : undefined;
}
/** A flat scope, not an ancestry cache. `null` is an explicit non-model boundary;
 * absence of a scope is reserved for instrumentation outside auto-owned work.
 * Only the current operation anchor and closest model candidate are retained.
 * Neither previous Contexts nor neutral ancestors are retained by this record. */
interface AutoModelScope {
  operation: AutoModelOperation | null;
  anchor?: object;
  owner?: object;
}
interface AutoScopeContext { base: Context; scope: AutoModelScope }
const SCOPES = Symbol.for("traceai.auto.model-scope-contexts.v1");
const scopeContexts: WeakMap<Context, AutoScopeContext> = globals[SCOPES] ?? (globals[SCOPES] = new WeakMap());
const MODEL_KINDS = new Set(["LLM", "EMBEDDING", "RERANKER", "GUARDRAIL"]);

function scopeSpan(value: any): any {
  // Preserve the own-data wrapper seam without invoking application accessors.
  const seen = new Set<object>();
  try {
    while (value && typeof value === "object" && !seen.has(value)) {
      seen.add(value);
      const wrapped = Object.getOwnPropertyDescriptor(value, "span");
      if (!wrapped || !("value" in wrapped) || !wrapped.value) break;
      value = wrapped.value;
    }
  } catch { /* Unsupported wrappers do not change application control flow. */ }
  return value;
}
function selectedSpan(ctx: Context): unknown {
  try { return trace.getSpan(ctx); } catch { return undefined; }
}
function spanKind(span: any): unknown {
  return span?.attributes?.["gen_ai.span.kind"] ?? span?.attributes?.["fi.span.kind"];
}
function advanceScope(scope: AutoModelScope, selected: unknown): AutoModelScope {
  try {
    // A retained candidate may still be changed by its application owner. Read
    // that classification live as well as its sink; never preserve a stale LLM
    // flag across a now-explicit TOOL boundary.
    if (scope.owner) {
      const previousKind = spanKind(scope.owner);
      if (previousKind !== undefined && previousKind !== null && !MODEL_KINDS.has(previousKind as string)) {
        scope = { operation: null, anchor: scope.owner };
      } else if (previousKind === undefined || previousKind === null) scope = { ...scope, owner: undefined };
    }
    const span = scopeSpan(selected);
    if (!span || span === scope.anchor) return scope;
    // A carrier or the MODEL's own SDK object belongs to the aggregate record,
    // not to an independent request. Steps/chunks use that same record.
    if (scope.operation && getAutoModelOperation(span) === scope.operation) return { ...scope, owner: undefined };
    const kind = spanKind(span);
    if (typeof kind === "string" && MODEL_KINDS.has(kind)) return { ...scope, owner: span };
    if (kind !== undefined && kind !== null) return { operation: null, anchor: span };
    return scope; // An unlabelled SDK transport is neutral, not a TOOL boundary.
  } catch { return scope; }
}

/** @internal Context authority for guard, supported-request selection and emit.
 * A closest candidate's CURRENT recording/pipeline state is consulted, never a
 * cached boolean. Opaque processors retain the documented onEnd contract only. */
export function autoModelResponsibility(ctx: Context): {
  scope: "operation" | "boundary";
  operation: AutoModelOperation | undefined;
  independentCoverage: boolean;
} | undefined {
  const entry = scopeContexts.get(ctx);
  const raw = entry ? undefined : ctx.getValue(OPERATION) as AutoModelOperation | null | undefined;
  if (!entry && raw === undefined) return undefined;
  const scope: AutoModelScope = entry ? advanceScope(entry.scope, selectedSpan(entry.base)) : { operation: raw ?? null };
  let independentCoverage = false;
  try {
    const owner: any = scope.owner;
    independentCoverage = !!owner && MODEL_KINDS.has(spanKind(owner) as string) &&
      owner.isRecording?.() === true && hasAutoSpanSink(owner._spanProcessor);
  } catch { /* No fabricated native responsibility. */ }
  return { scope: scope.operation ? "operation" : "boundary", operation: scope.operation ?? undefined, independentCoverage };
}

function scopeContext(base: Context, scope: AutoModelScope): Context {
  // Delegate to the original immutable Context implementation, not to our last
  // wrapper. The shared weak registry makes CJS/ESM re-entry flatten identically.
  // The pinned SDKs use trace.setSpan(parent, child) -> parent.setValue(...), so
  // every neutral descendant carries this scope even without any onStart call.
  const result: Context = {
    getValue(key) {
      return key === OPERATION ? advanceScope(scope, selectedSpan(base)).operation : base.getValue(key);
    },
    setValue(key, value) {
      const next = base.setValue(key, value);
      if (key === OPERATION) return scopeContext(next, { operation: value as AutoModelOperation | null, anchor: scopeSpan(selectedSpan(next)) });
      const previous = selectedSpan(base), selected = selectedSpan(next);
      const inherited = previous === selected ? scope : advanceScope(scope, previous);
      return scopeContext(next, previous && !selected ? { operation: null } : inherited);
    },
    deleteValue(key) {
      const next = base.deleteValue(key);
      if (key === OPERATION) return next; // Explicit removal leaves the owned scope.
      const previous = selectedSpan(base), selected = selectedSpan(next);
      return scopeContext(next, previous && !selected ? { operation: null } : scope);
    },
  };
  scopeContexts.set(result, { base, scope });
  return result;
}

/** Enter a MODEL operation or an explicit TOOL/AGENT/non-model boundary.
 * Supported seam: immutable Context getValue/setValue/deleteValue, including
 * trace.setSpan/setSpanContext and SDK startActiveSpan using that parent Context.
 * Kind is declared while selected or before descending; unrelated ROOT_CONTEXT
 * replacement, custom contexts that discard keys, and retroactive classification
 * after leaving a neutral scope are not inferred from parent IDs. No API globals,
 * tracer/span methods, attributes, resource, sampler or context manager are patched.
 * There is no installed hook to uninstall; metadata dies with its owned contexts. */
export function withAutoModelOperation(ctx: Context, operation?: AutoModelOperation): Context {
  const base = (scopeContexts.get(ctx)?.base ?? ctx).setValue(OPERATION, operation ?? null);
  return scopeContext(base, { operation: operation ?? null, anchor: scopeSpan(selectedSpan(base)) });
}
export function activeAutoModelOperation(ctx: Context): AutoModelOperation | undefined {
  return (ctx.getValue(OPERATION) as AutoModelOperation | null | undefined) ?? undefined;
}
/** Read-only compatibility metadata. Production model routing uses the record above. */
export function rememberAutoModelCoverage(span: object, ready: () => boolean): void { coverage.set(span, ready); }
export function hasAutoModelCoverage(span: object): boolean {
  try {
    const value = coverage.get(span);
    return typeof value === "function" ? value() === true : value?.isCovered() === true;
  } catch { return false; }
}
