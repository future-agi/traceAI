import {
  InstrumentationBase,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import { context, createContextKey, diag, Span, SpanKind, SpanStatusCode, trace, TracerProvider } from "@opentelemetry/api";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { PipecatEventSource } from "./client";
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
  delegatedParams?: { value: unknown };
};
type FinishSpan = (code: SpanStatusCode) => void;
type DisconnectScope = { instance: object; active: boolean; pending: Set<FinishSpan> };
const CONNECTION_SCOPE = createContextKey("traceai.pipecat.connection-scope");
const DISCONNECT_SCOPE = createContextKey("traceai.pipecat.disconnect-scope");
const owners = new WeakMap<object, PipecatInstrumentation>();
let patchedCount = 0;

/** Node operation tracing. For browser/ESM event tracing use observePipecatClient. */
export class PipecatInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private traceConfig?: TraceConfigOptions;
  private patches?: Map<ClientPrototype, string[]>;
  private manualModules?: Set<unknown>;
  // Iterable so unpatch/disable can finish spans and remove only our listeners.
  private pendingConnections = new Map<object, Set<FinishSpan>>();

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
        for (const finish of pending) finish(SpanStatusCode.ERROR);
      }
    }
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

  private observeConnection(scope: ConnectionScope, finish: FinishSpan): () => void {
    const events = scope.instance as Partial<PipecatEventSource>;
    if (typeof events.on !== "function" || typeof events.off !== "function") return () => {};
    // While an endpoint is starting a bot, a callback can make a separate
    // connection. Its bot-ready event is not this endpoint call's handshake.
    const ready = () => { if (scope.handshaking) finish(SpanStatusCode.OK); };
    // Bot-ready resolves the SDK promise before invoking application callbacks.
    // Let that synchronous dispatch complete if a callback disconnects immediately.
    const disconnected = () => {
      const disconnect = context.active().getValue(DISCONNECT_SCOPE) as DisconnectScope | undefined;
      // Constructor callbacks run before the public event. A retry they create
      // can already be listening, but does not belong to the old disconnect.
      if (disconnect?.active && disconnect.instance === scope.instance && !disconnect.pending.has(finish)) return;
      queueMicrotask(() => finish(SpanStatusCode.ERROR));
    };
    const detach = () => {
      for (const [event, listener] of [["botReady", ready], ["disconnected", disconnected]] as const) {
        try { events.off!(event, listener); }
        catch { diag.warn("Pipecat client lifecycle telemetry cleanup failed"); }
      }
    };
    try {
      events.on("botReady", ready);
      events.on("disconnected", disconnected);
    } catch {
      detach();
      diag.warn("Pipecat client lifecycle telemetry failed");
    }
    return detach;
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
      return context.with(parentContext.deleteValue(CONNECTION_SCOPE), () => original.apply(instance, args));
    }
    const disconnect: DisconnectScope | undefined = operation === "client.disconnect" && client
      ? { instance: client, active: true, pending: new Set(this.pendingConnections.get(client)) } : undefined;
    // Snapshot attempts now; a disconnected callback may start a new connection.
    for (const finish of disconnect?.pending ?? []) queueMicrotask(() => finish(SpanStatusCode.ERROR));
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
    let detach = () => {};
    let finished = false;
    const finish: FinishSpan = (code) => {
      if (finished) return;
      finished = true;
      detach();
      if (scope) {
        const pending = this.pendingConnections.get(scope.instance);
        pending?.delete(finish);
        if (!pending?.size) this.pendingConnections.delete(scope.instance);
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
    if (disconnect) operationContext = operationContext.setValue(DISCONNECT_SCOPE, disconnect);
    if (scope) {
      // A retry's transport has its own lifecycle, not the enclosing disconnect's.
      operationContext = operationContext.deleteValue(DISCONNECT_SCOPE);
      const pending = this.pendingConnections.get(scope.instance) ?? new Set<FinishSpan>();
      pending.add(finish);
      this.pendingConnections.set(scope.instance, pending);
      detach = this.observeConnection(scope, finish);
      operationContext = operationContext.setValue(CONNECTION_SCOPE, scope);
    }
    const settle = (code: SpanStatusCode) => {
      if (disconnect) {
        disconnect.active = false;
        disconnect.pending.clear();
      }
      // A cancelled endpoint call may still delegate later. Keep its one-use
      // delegation slot until SDK settlement, without retaining event listeners.
      if (scope) {
        scope.active = false;
        delete scope.delegatedParams;
      }
      finish(code);
    };
    try {
      const result = context.with(operationContext, () => {
        try { return original.apply(instance, args); }
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
