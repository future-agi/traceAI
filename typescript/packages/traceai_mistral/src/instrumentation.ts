import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import { context, diag, trace, SpanKind, SpanStatusCode, TracerProvider } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { FITracer, TraceConfigOptions, TraceConfig, generateTraceConfig, withSafety } from "@traceai/fi-core";
import { VERSION } from "./version";
import { inputAttributes, outputAttributes, record, StreamOutput } from "./attributes";
import { observeStream } from "./stream";

const MODULE_NAME = "@mistralai/mistralai";
// Telemetry is best effort. Do not put request content from an exception into logs.
function telemetry<T>(work: () => T): T | null {
  return withSafety({ fn: work, onError: () => diag.warn("Mistral instrumentation could not record telemetry.") })();
}
type Method = (this: unknown, ...args: unknown[]) => unknown;
type Target = Record<string, unknown>;
type Operation = "chat" | "embeddings" | "conversation";
interface Patch {
  target: Target;
  key: string;
  descriptor?: PropertyDescriptor;
  original: Method;
  wrapped: Method;
  attached: boolean;
}
// Share the marker across CommonJS/ESM copies to avoid double instrumentation.
const WRAPPED = Symbol.for("@traceai/mistral/wrapped");
const activePatches = new Set<WeakRef<Patch>>();
const collectedPatches = new FinalizationRegistry<{ references: Set<WeakRef<Patch>>; reference: WeakRef<Patch> }>(({ references, reference }) => {
  references.delete(reference);
  activePatches.delete(reference);
});

/** Whether any live Mistral methods are currently instrumented. */
export function isPatched(): boolean {
  for (const reference of activePatches) {
    const patch = reference.deref();
    if (!patch) activePatches.delete(reference);
    else if (patch.attached) return true;
  }
  return false;
}

const RESOURCES: ReadonlyArray<readonly [readonly string[], readonly string[], Operation]> = [
  [["chat"], ["complete", "stream", "parse", "parseStream"], "chat"],
  [["fim"], ["complete", "stream"], "chat"],
  [["agents"], ["complete", "stream"], "chat"],
  [["embeddings"], ["create"], "embeddings"],
  [["beta", "conversations"], ["start", "append", "restart", "startStream", "appendStream", "restartStream"], "conversation"],
];

export class MistralInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private traceConfig?: TraceConfigOptions;
  private resolvedConfig!: TraceConfig;
  // InstrumentationBase can call enable()/patch() before the derived constructor runs.
  private patches?: Set<WeakRef<Patch>>;
  // Client registrations must not keep per-request clients or their options alive.
  private registrations?: WeakMap<Target, Map<string, Patch>>;
  public readonly instrumentationName = "@traceai/mistral";

  constructor({ instrumentationConfig, traceConfig }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/mistral", VERSION, { ...instrumentationConfig });
    this.traceConfig = traceConfig;
    this.refreshTracer();
  }

  private refreshTracer(): void {
    this.resolvedConfig = generateTraceConfig(this.traceConfig);
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this.resolvedConfig });
  }

  public override setTracerProvider(provider: TracerProvider): void {
    super.setTracerProvider(provider);
    this.refreshTracer();
  }

  public override enable(): void {
    super.enable();
    this.refreshTracer();
    for (const reference of this.patches ?? []) {
      const patch = reference.deref();
      if (patch) this.attach(patch, reference);
    }
  }

  public override disable(): void {
    this.unpatch();
    super.disable();
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      MODULE_NAME, [">=0.1.0"], this.patch.bind(this), this.unpatch.bind(this),
    );
  }

  /** Register an imported SDK module, including a read-only ESM namespace. */
  public manuallyInstrument(module: Record<string, unknown>): void {
    this.patch(module);
  }

  /**
   * Instrument a real client without replacing it or its options. Also supports
   * legacy default-export SDKs with instance methods (including 0.x and 1.0.0).
   */
  public instrumentClient<T extends object>(client: T): T {
    this.instrumentResources(client as Target, false);
    for (const [key, operation] of [
      ["chat", "chat"], ["chatStream", "chat"], ["embeddings", "embeddings"],
    ] as const) {
      this.register(client as Target, key, operation, `Mistral ${key}`);
    }
    return client;
  }

  private patch(module: Record<string, unknown>) {
    const Mistral = module.Mistral;
    if (typeof Mistral === "function") {
      // Discover shared resource *classes*, not resources cached by getters on
      // Mistral.prototype. No API call is made and no environment key is read.
      const probe = new (Mistral as new (options: { apiKey: string }) => Target)({ apiKey: "" });
      this.instrumentResources(probe, true);
    } else if (typeof module.default === "function") {
      diag.warn("Legacy default-export Mistral SDKs require instrumentClient(client).");
    }
    return module;
  }

  private instrumentResources(client: Target, prototypes: boolean): void {
    for (const [path, methods, operation] of RESOURCES) {
      let resource: unknown = client;
      for (const key of path) resource = record(resource)[key];
      if (resource == null || typeof resource !== "object") continue;
      for (const method of methods) {
        let target = resource as Target;
        if (prototypes) {
          // Find the class defining the method; do not mutate the probe itself.
          target = Object.getPrototypeOf(target) as Target;
          while (target && !Object.prototype.hasOwnProperty.call(target, method)) {
            target = Object.getPrototypeOf(target) as Target;
          }
        }
        if (target) this.register(target, method, operation, `Mistral ${path.join(".")}.${method}`);
      }
    }
  }

  private register(target: Target, key: string, operation: Operation, name: string): void {
    const original = target[key];
    if (typeof original !== "function" || Reflect.get(original, WRAPPED)) return;
    this.patches ??= new Set();
    this.registrations ??= new WeakMap();
    const methods = this.registrations.get(target) ?? new Map<string, Patch>();
    if (methods.has(key)) return;
    const instrumentation = this;
    const wrapped: Method = function (...args) {
      if (!instrumentation.isEnabled() || isTracingSuppressed(context.active())) {
        return original.apply(this, args);
      }
      const input = typeof args[0] === "string"
        ? { model: args[0], [operation === "embeddings" ? "inputs" : "messages"]: args[1] }
        : args[0];
      const span = telemetry(() => instrumentation.fiTracer.startSpan(name, {
        kind: SpanKind.INTERNAL,
        attributes: inputAttributes(input, operation, instrumentation.resolvedConfig),
      }));
      if (!span) return original.apply(this, args);
      const spanContext = trace.setSpan(context.active(), span);
      let ended = false;
      const finish = (error?: unknown) => {
        if (ended) return;
        ended = true;
        telemetry(() => {
          if (error !== undefined) {
            span.recordException(error instanceof Error ? error : String(error));
            span.setStatus({ code: SpanStatusCode.ERROR, message: error instanceof Error ? error.message : String(error) });
          } else {
            span.setStatus({ code: SpanStatusCode.OK });
          }
        });
        telemetry(() => span.end());
      };
      const onResult = (result: unknown): unknown => {
        if (result && typeof Reflect.get(Object(result), Symbol.asyncIterator) === "function") {
          const output = new StreamOutput(instrumentation.resolvedConfig);
          return observeStream(result as AsyncIterable<unknown>, spanContext,
            chunk => { telemetry(() => output.add(chunk)); },
            error => {
              telemetry(() => span.setAttributes(output.attributes()));
              finish(error ?? output.error);
            });
        }
        telemetry(() => span.setAttributes(outputAttributes(result, operation, instrumentation.resolvedConfig)));
        finish();
        return result;
      };
      try {
        const result = context.with(spanContext, () => original.apply(this, args));
        if (result && typeof record(result).then === "function") {
          return Promise.resolve(result).then(onResult, error => { finish(error); throw error; });
        }
        return onResult(result);
      } catch (error) {
        finish(error);
        throw error;
      }
    };
    Object.defineProperty(wrapped, WRAPPED, { value: true });
    const patch: Patch = {
      target, key, original: original as Method, wrapped,
      descriptor: Object.getOwnPropertyDescriptor(target, key), attached: false,
    };
    methods.set(key, patch);
    this.registrations.set(target, methods);
    const reference = new WeakRef(patch);
    this.patches.add(reference);
    collectedPatches.register(patch, { references: this.patches, reference });
    if (this.isEnabled()) this.attach(patch, reference);
  }

  private attach(patch: Patch, reference: WeakRef<Patch>): void {
    if (patch.attached || patch.target[patch.key] !== patch.original) return;
    Object.defineProperty(patch.target, patch.key, {
      configurable: true, writable: true, enumerable: patch.descriptor?.enumerable ?? false,
      value: patch.wrapped,
    });
    patch.attached = true;
    activePatches.add(reference);
  }

  private unpatch(): void {
    for (const reference of this.patches ?? []) {
      const patch = reference.deref();
      activePatches.delete(reference);
      if (!patch?.attached) continue;
      // Never remove a wrapper installed by another instrumentor afterwards.
      if (patch.target[patch.key] === patch.wrapped) {
        if (patch.descriptor) Object.defineProperty(patch.target, patch.key, patch.descriptor);
        else delete patch.target[patch.key];
      }
      patch.attached = false;
    }
  }
}
