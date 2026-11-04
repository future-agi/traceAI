import {
  InstrumentationBase,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import { context, createContextKey, diag, Span, SpanKind, SpanStatusCode, trace, TracerProvider } from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { VERSION } from "./version";

export interface PipecatInstrumentationConfig {
  instrumentationConfig?: Record<string, unknown>;
  traceConfig?: TraceConfigOptions;
}

type ClientMethod = (...args: unknown[]) => unknown;
type ClientPrototype = Record<string, ClientMethod>;
const METHODS = {
  connect: "client.connect",
  disconnect: "client.disconnect",
  action: "action",
  sendMessage: "send_message",
  sendClientMessage: "client.send_message",
  sendClientRequest: "client.request",
  sendText: "client.send_text",
} as const;
type ConnectionScope = {
  instance: object;
  active: boolean;
  invoking: boolean;
  startingBot: boolean;
  handshaking: boolean;
  resolver?: unknown;
  delegatedParams?: { value: unknown };
};
type FinishSpan = (code: SpanStatusCode) => void;
type PendingConnections = {
  attempts: Map<FinishSpan, ConnectionScope>;
  claimedResolvers: WeakSet<object>;
  connected: boolean;
  successes: number;
  disconnections: Map<FinishSpan, ConnectionScope>[];
  detach: () => void;
};
const CONNECTION_SCOPE = createContextKey("traceai.pipecat.connection-scope");
const owners = new WeakMap<object, PipecatInstrumentation>();
let patchedCount = 0;

/** Node operation tracing. For browser/ESM event tracing use observePipecatClient. */
export class PipecatInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private traceConfig?: TraceConfigOptions;
  private patches?: Map<ClientPrototype, string[]>;
  private manualModules?: Set<unknown>;
  // Iterable so unpatch/disable can finish spans and restore owned callbacks.
  private pendingConnections = new Map<object, PendingConnections>();
  private initializingTransports = new WeakSet<object>();

  constructor(config: PipecatInstrumentationConfig = {}) {
    super("@traceai/fi-instrumentation-pipecat", VERSION, config.instrumentationConfig ?? {});
    this.traceConfig = config.traceConfig;
    this.refreshTracer();
  }

  private refreshTracer(): void {
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this.traceConfig });
  }

  override setTracerProvider(provider: TracerProvider): void {
    super.setTracerProvider(provider);
    this.refreshTracer();
  }

  override enable(): void {
    super.enable();
    this.refreshTracer();
    for (const module of this.manualModules ?? []) this.patch(module);
  }

  override disable(): void {
    super.disable();
    for (const prototype of this.patches?.keys() ?? []) this.unpatchPrototype(prototype);
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "@pipecat-ai/client-js",
      ["^0.3.0", "^1.16.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
  }

  /** Supports CommonJS modules and immutable ESM namespace objects. */
  manuallyInstrument(pipecatModule: unknown): void {
    if (pipecatModule == null) return;
    (this.manualModules ??= new Set()).add(pipecatModule);
    if (this.isEnabled()) this.patch(pipecatModule);
  }

  private prototypes(module: unknown): ClientPrototype[] {
    if (!module || typeof module !== "object") return [];
    const exports = module as Record<string, { prototype?: ClientPrototype } | undefined>;
    return [exports.RTVIClient?.prototype, exports.PipecatClient?.prototype]
      .filter((prototype): prototype is ClientPrototype => !!prototype);
  }

  private patch(module: unknown): unknown {
    for (const prototype of this.prototypes(module)) {
      if (owners.has(prototype)) continue;
      const methods: string[] = [];
      for (const [method, operation] of Object.entries(METHODS)) {
        if (typeof prototype[method] !== "function") continue;
        const instrumentation = this;
        this._wrap(prototype, method, (original: ClientMethod) => function (this: unknown, ...args: unknown[]) {
          return instrumentation.traceOperation(original, this, args, operation);
        });
        methods.push(method);
      }
      // Follow the SDK's endpoint delegation, without reading connection arguments.
      // These helpers add no spans of their own.
      for (const method of ["startBotAndConnect", "startBot"] as const) {
        if (typeof prototype[method] !== "function") continue;
        const instrumentation = this;
        this._wrap(prototype, method, (original: ClientMethod) => function (this: unknown, ...args: unknown[]) {
          return instrumentation.followDelegation(original, this, args, method);
        });
        methods.push(method);
      }
      // Legacy disconnect() reinitializes the transport after its close callbacks.
      // initialize()'s default state is not another close boundary: cancellation
      // still belongs to that disconnect's captured attempts, not a callback retry.
      if (typeof prototype._initialize === "function") {
        const instrumentation = this;
        this._wrap(prototype, "_initialize", (original: ClientMethod) => function (this: object, ...args: unknown[]) {
          const initializing = instrumentation.initializingTransports;
          const nested = initializing.has(this);
          initializing.add(this);
          try { return original.apply(this, args); }
          finally { if (!nested) initializing.delete(this); }
        });
        methods.push("_initialize");
      }
      if (methods.length) {
        (this.patches ??= new Map()).set(prototype, methods);
        owners.set(prototype, this);
        patchedCount++;
      }
    }
    return module;
  }

  private unpatch(module: unknown): void {
    for (const prototype of this.prototypes(module)) this.unpatchPrototype(prototype);
  }

  private unpatchPrototype(prototype: ClientPrototype): void {
    if (owners.get(prototype) !== this) return;
    for (const method of this.patches?.get(prototype) ?? []) this._unwrap(prototype, method);
    this.patches?.delete(prototype);
    owners.delete(prototype);
    patchedCount--;
    for (const [client, pending] of this.pendingConnections ?? []) {
      if (Object.prototype.isPrototypeOf.call(prototype, client)) {
        for (const finish of pending.attempts.keys()) finish(SpanStatusCode.ERROR);
        pending.connected = false;
        pending.disconnections.length = 0;
        this.releaseConnections(client, pending);
      }
    }
  }

  private releaseConnections(client: object, pending: PendingConnections): void {
    // A connected client still needs the state boundary even with no pending
    // spans: a later disconnected-state callback can start a new handshake.
    if (pending.attempts.size || pending.connected || pending.disconnections.length) return;
    pending.detach();
    if (this.pendingConnections.get(client) === pending) this.pendingConnections.delete(client);
  }

  private followDelegation(original: ClientMethod, instance: unknown, args: unknown[], method: "startBotAndConnect" | "startBot"): unknown {
    const scope = context.active().getValue(CONNECTION_SCOPE) as ConnectionScope | undefined;
    if (!scope?.active || !scope.invoking || scope.instance !== instance) return original.apply(instance, args);
    if (method === "startBotAndConnect") {
      scope.handshaking = false;
      scope.startingBot = true;
      try { return original.apply(instance, args); }
      finally { scope.startingBot = false; }
    }
    if (!scope.startingBot) return original.apply(instance, args);
    // startBot invokes application lifecycle callbacks. Their asynchronous work
    // must not inherit the internal delegation slot; the SDK's awaiting caller does.
    const result = context.with(context.active().deleteValue(CONNECTION_SCOPE), () => original.apply(instance, args));
    if (result instanceof Promise) {
      return result.then(value => {
        // Arm only after startBot's callbacks return, and match the returned object
        // by identity. A callback's independent connect must not consume this slot.
        if (scope.active) scope.delegatedParams = { value };
        return value;
      });
    }
    return result;
  }

  private observeConnections(client: object, pending: PendingConnections): () => void {
    const restore: (() => void)[] = [];
    const detach = () => {
      for (const undo of restore) {
        try { undo(); }
        catch { diag.warn("Pipecat client lifecycle telemetry cleanup failed"); }
      }
    };
    try {
      // Both supported SDKs share this callback table with the transport. Observe
      // its dispatch boundary, BEFORE constructor callbacks and public listeners:
      // either may synchronously create a retry belonging to a new attempt.
      // This internal SDK contract is exercised with the real floor/latest clients.
      const callbacks = (client as { _options?: { callbacks?: Record<string, ClientMethod> } })._options?.callbacks;
      if (!callbacks) return detach;
      const instrumentation = this;
      for (const name of ["onBotReady", "onDisconnected", "onTransportStateChanged"] as const) {
        const original = callbacks[name];
        if (typeof original !== "function") continue;
        const wrapped: ClientMethod = function (this: unknown, ...args: unknown[]) {
          // Exclude only the SDK's synchronous initialize dispatch. User callbacks
          // can cause genuine nested drops, and must not inherit this exclusion.
          const initializing = instrumentation.initializingTransports.delete(client);
          let disconnected: Map<FinishSpan, ConnectionScope> | undefined;
          try {
            if (name === "onBotReady") {
              // Both SDKs resolve this exact function before dispatching bot-ready.
              // A broadcast must not mark older, replaced handshakes successful.
              // Finish now so a callback can disconnect and dispose its provider
              // without racing promise continuations or a cleanup timer.
              const resolver = PipecatInstrumentation.connectionResolver(client);
              for (const [finish, scope] of pending.attempts) {
                if (scope.resolver && scope.resolver === resolver) finish(SpanStatusCode.OK);
              }
            } else if (name === "onTransportStateChanged" && args[0] === "disconnected" && !initializing) {
              // The state event precedes onDisconnected. Capture even an EMPTY
              // set before application callbacks can install a replacement resolver.
              disconnected = new Map(pending.attempts);
              pending.connected = false;
              pending.disconnections.push(disconnected);
              PipecatInstrumentation.cancelConnections(disconnected);
            } else if (name === "onDisconnected") {
              // Nested synchronous closes notify in stack order. Transports that
              // notify without a state event still cancel the current attempts.
              const closed = pending.disconnections.pop();
              if (!closed) pending.connected = false;
              PipecatInstrumentation.cancelConnections(closed ?? new Map(pending.attempts));
              instrumentation.releaseConnections(client, pending);
            }
            return original.apply(this, args);
          } catch (error) {
            if (disconnected) {
              // A throwing callback/listener aborts transport dispatch before
              // onDisconnected. Retire only this boundary, preserving live retries.
              const index = pending.disconnections.indexOf(disconnected);
              if (index !== -1) pending.disconnections.splice(index, 1);
              instrumentation.releaseConnections(client, pending);
            }
            throw error;
          } finally {
            if (initializing) instrumentation.initializingTransports.add(client);
          }
        };
        callbacks[name] = wrapped;
        restore.push(() => { if (callbacks[name] === wrapped) callbacks[name] = original; });
      }
    } catch {
      detach();
      diag.warn("Pipecat client lifecycle telemetry failed");
    }
    return detach;
  }

  private static cancelConnections(attempts: Map<FinishSpan, ConnectionScope>): void {
    for (const finish of attempts.keys()) finish(SpanStatusCode.ERROR);
  }

  private static connectionResolver(client: object): unknown {
    // The supported floor/latest SDKs assign their resolver synchronously, then
    // invoke it immediately before onBotReady. Compare identity only: never call,
    // replace, serialize or export it. Real-client tests pin this internal contract.
    try {
      const target = client as { _connectResolve?: unknown; _startResolve?: unknown };
      return target._connectResolve ?? target._startResolve;
    } catch {
      return undefined;
    }
  }

  private invokeConnection(original: ClientMethod, instance: unknown, args: unknown[], scope?: ConnectionScope): unknown {
    const pending = scope && this.pendingConnections.get(scope.instance);
    const before = scope && PipecatInstrumentation.connectionResolver(scope.instance);
    const result = original.apply(instance, args);
    if (scope?.handshaking && pending) {
      const resolver = PipecatInstrumentation.connectionResolver(scope.instance);
      // A rejected overlapping call never installs a resolver of its own. An
      // initialization callback can also reenter connect before this call returns:
      // the nested attempt claims its resolver first, so the outer cannot borrow it.
      if (typeof resolver === "function" && resolver !== before && !pending.claimedResolvers.has(resolver)) {
        pending.claimedResolvers.add(resolver);
        scope.resolver = resolver;
      }
    }
    return result;
  }

  private traceOperation(original: ClientMethod, instance: unknown, args: unknown[], operation: string): unknown {
    const client = instance !== null && (typeof instance === "object" || typeof instance === "function") ? instance : undefined;
    const parentContext = context.active();
    const inherited = parentContext.getValue(CONNECTION_SCOPE) as ConnectionScope | undefined;
    if (operation === "client.connect" && inherited?.active && inherited.instance === client &&
        inherited.delegatedParams && inherited.delegatedParams.value === args[0]) {
      // Exactly one SDK-delegated connect(params), never all same-client calls
      // in an inherited asynchronous context (which also includes user retries).
      delete inherited.delegatedParams;
      inherited.handshaking = true;
      return context.with(parentContext.deleteValue(CONNECTION_SCOPE), () => this.invokeConnection(original, instance, args, inherited));
    }
    // Only successful disconnects (or actual transport notifications) cancel
    // attempts. A rejected disconnect can leave the handshake running normally.
    const disconnectOwner = operation === "client.disconnect" && client
      ? this.pendingConnections.get(client) : undefined;
    const disconnectSuccesses = disconnectOwner?.successes;
    const disconnect = operation === "client.disconnect" && client
      ? new Map(disconnectOwner?.attempts) : undefined;
    let span: Span;
    try {
      span = this.fiTracer.startSpan(`Pipecat ${operation}`, {
        kind: SpanKind.CLIENT,
        attributes: {
          "fi.span.kind": "CHAIN",
          "pipecat.telemetry_source": "client",
          "pipecat.operation": operation,
        },
      });
    } catch {
      diag.warn("Pipecat client operation telemetry failed");
      return original.apply(instance, args);
    }
    const scope: ConnectionScope | undefined = operation === "client.connect" && client
      ? { instance: client, active: true, invoking: true, startingBot: false, handshaking: true } : undefined;
    let finished = false;
    const finish: FinishSpan = (code) => {
      if (finished) return;
      finished = true;
      if (scope) {
        const pending = this.pendingConnections.get(scope.instance);
        pending?.attempts.delete(finish);
        if (pending) {
          if (code === SpanStatusCode.OK) {
            pending.connected = true;
            pending.successes++;
          }
          this.releaseConnections(scope.instance, pending);
        }
      }
      try {
        // Error messages and arguments may contain credentials or transcripts.
        span.setStatus({ code });
        span.end();
      } catch {
        diag.warn("Pipecat client operation telemetry failed");
      }
    };
    let operationContext = trace.setSpan(parentContext, span);
    if (scope) {
      let pending = this.pendingConnections.get(scope.instance);
      if (!pending) {
        pending = {
          attempts: new Map(), claimedResolvers: new WeakSet(),
          connected: false, successes: 0, disconnections: [], detach: () => {},
        };
        this.pendingConnections.set(scope.instance, pending);
        pending.detach = this.observeConnections(scope.instance, pending);
      }
      pending.attempts.set(finish, scope);
      operationContext = operationContext.setValue(CONNECTION_SCOPE, scope);
    }
    const settle = (code: SpanStatusCode) => {
      if (disconnect) {
        if (code === SpanStatusCode.OK) {
          PipecatInstrumentation.cancelConnections(disconnect);
          if (client && disconnectOwner && this.pendingConnections.get(client) === disconnectOwner) {
            // Also release observers when a successful disconnect omits callbacks,
            // but never retire a connection completed by a reentrant retry.
            if (disconnectOwner.successes === disconnectSuccesses) disconnectOwner.connected = false;
            if (!disconnectOwner.attempts.size && !disconnectOwner.connected) {
              disconnectOwner.disconnections.length = 0;
              this.releaseConnections(client, disconnectOwner);
            }
          }
        }
        disconnect.clear();
      }
      // A cancelled endpoint call may still delegate later. Keep its one-use
      // delegation slot until SDK settlement, without retaining callback wrappers.
      if (scope) {
        scope.active = false;
        delete scope.delegatedParams;
      }
      finish(code);
    };
    try {
      const result = context.with(operationContext, () => {
        try { return this.invokeConnection(original, instance, args, scope); }
        finally { if (scope) scope.invoking = false; }
      });
      if (result instanceof Promise) {
        return result.then(
          (value) => { settle(SpanStatusCode.OK); return value; },
          (error: unknown) => { settle(SpanStatusCode.ERROR); throw error; },
        );
      }
      settle(SpanStatusCode.OK);
      return result;
    } catch (error) {
      settle(SpanStatusCode.ERROR);
      throw error;
    }
  }
}

/** Whether a client prototype currently has operation tracing installed. */
export function isPatched(): boolean { return patchedCount > 0; }

/** @deprecated Disable instrumentation instances to restore their patches. */
export function _resetPatchedStateForTesting(): void {
  // Retained for compatibility. Clearing ownership without unwrapping would duplicate spans.
}
