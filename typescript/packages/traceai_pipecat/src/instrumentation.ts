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
type ConnectionScope = { instance: object; active: boolean };
type FinishSpan = (code: SpanStatusCode) => void;
const CONNECTION_SCOPES = createContextKey("traceai.pipecat.connection-scopes");
const owners = new WeakMap<object, PipecatInstrumentation>();
let patchedCount = 0;

/** Node operation tracing. For browser/ESM event tracing use observePipecatClient. */
export class PipecatInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private traceConfig?: TraceConfigOptions;
  private patches?: Map<ClientPrototype, string[]>;
  private manualModules?: Set<unknown>;
  private pendingConnections = new WeakMap<object, Set<FinishSpan>>();

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
  }

  private traceOperation(original: ClientMethod, instance: unknown, args: unknown[], operation: string): unknown {
    const client = instance !== null && (typeof instance === "object" || typeof instance === "function") ? instance : undefined;
    const parentContext = context.active();
    const scopes = (parentContext.getValue(CONNECTION_SCOPES) as ConnectionScope[] | undefined) ?? [];
    // connect(endpoint) asynchronously delegates to connect(params) on the same client.
    // Do not suppress another client or a separate call outside this async scope.
    if (operation === "client.connect" && scopes.some(scope => scope.active && scope.instance === client)) {
      return original.apply(instance, args);
    }
    if (operation === "client.disconnect" && client) {
      // Both supported SDKs can leave the bot-ready promise pending after disconnect.
      for (const finish of this.pendingConnections.get(client) ?? []) finish(SpanStatusCode.ERROR);
    }
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
    const scope = operation === "client.connect" && client ? { instance: client, active: true } : undefined;
    let finished = false;
    const finish: FinishSpan = (code) => {
      if (finished) return;
      finished = true;
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
    if (scope) {
      const pending = this.pendingConnections.get(scope.instance) ?? new Set<FinishSpan>();
      pending.add(finish);
      this.pendingConnections.set(scope.instance, pending);
      operationContext = operationContext.setValue(CONNECTION_SCOPES, [...scopes, scope]);
    }
    const settle = (code: SpanStatusCode) => {
      // Cancellation ends the span, but recursion stays suppressed until the SDK settles.
      if (scope) scope.active = false;
      finish(code);
    };
    try {
      const result = context.with(operationContext, () => original.apply(instance, args));
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
