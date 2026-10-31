import { InstrumentationBase, InstrumentationConfig, InstrumentationNodeModuleDefinition } from "@opentelemetry/instrumentation";
import { context, createContextKey, diag, trace, SpanKind, SpanStatusCode, Span } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { FITracer, generateTraceConfig, TraceConfigOptions } from "@traceai/fi-core";
import { VERSION } from "./version";
import { Data, Operation, object, inputAttributes, outputAttributes, hideOutput } from "./attributes";
import { wrapStream } from "./stream";

const OPERATIONS: Operation[] = ["chat", "chatStream", "embed", "rerank"];
const IN_COHERE_CALL = createContextKey("traceai.cohere.call");
type Method = (this: unknown, ...args: unknown[]) => unknown;
type Patch = { target: Data; method: Operation; wrapped: Method; hadOwn: boolean };
const owners = new WeakMap<object, Map<Operation, CohereInstrumentation>>();
let patchCount = 0;

/** True when at least one SDK method is instrumented, across module copies. */
export function isPatched(): boolean { return patchCount > 0; }

/** Cohere v1/v2 instrumentation. Install before constructing SDK clients. */
export class CohereInstrumentation extends InstrumentationBase {
  private _traceConfig?: TraceConfigOptions;
  // InstrumentationBase may call enable/patch during super(), so initialize lazily.
  private patches?: Patch[];
  private manualModules?: Set<Data>;
  public readonly instrumentationName = "@traceai/cohere";

  constructor({ instrumentationConfig, traceConfig }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/cohere", VERSION, { ...instrumentationConfig });
    this._traceConfig = traceConfig;
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition("cohere-ai", [">=7.0.0 <9"], this.patch.bind(this), this.unpatch.bind(this));
  }

  public override enable(): void {
    super.enable();
    for (const module of this.manualModules ?? []) this.patch(module);
  }

  public override disable(): void {
    super.disable();
    this.restorePatches();
  }

  manuallyInstrument(module: Data): void {
    (this.manualModules ??= new Set()).add(module);
    if (this.isEnabled()) this.patch(module);
  }

  private patch(module: Data): Data {
    const Client = module.CohereClient as { prototype?: Data; new(options: { token: string }): { v2?: object } } | undefined;
    const targets = [Client?.prototype];
    // V2Client is not exported at the module root. The public client.v2 getter
    // exposes the same generated class used by CohereClientV2's bound methods.
    // Construction performs no I/O; an explicit inert token avoids env lookup.
    if (typeof Client === "function" && Client.prototype && "v2" in Client.prototype) {
      try {
        const v2 = new Client({ token: "traceai-instrumentation-placeholder" }).v2;
        if (v2) targets.push(Object.getPrototypeOf(v2) as Data);
      } catch {
        // Telemetry must not prevent module loading; v1 remains usable.
        diag.warn("Cohere instrumentation could not discover the v2 client");
      }
    }
    for (const target of targets) {
      if (!target) continue;
      for (const method of OPERATIONS) {
        if (typeof target[method] !== "function" || owners.get(target)?.has(method)) continue;
        const hadOwn = Object.prototype.hasOwnProperty.call(target, method);
        this._wrap(target, method, (original) => this.wrapMethod(method, original as Method));
        const wrapped = target[method] as Method;
        (this.patches ??= []).push({ target, method, wrapped, hadOwn });
        if (!owners.has(target)) owners.set(target, new Map());
        owners.get(target)!.set(method, this);
        patchCount++;
      }
    }
    return module;
  }

  private unpatch(): void { this.restorePatches(); }

  private restorePatches(): void {
    for (const { target, method, wrapped, hadOwn } of this.patches ?? []) {
      if (target[method] === wrapped) {
        this._unwrap(target, method);
        if (!hadOwn) delete target[method];
      }
      owners.get(target)?.delete(method);
      patchCount--;
    }
    this.patches = [];
  }

  private wrapMethod(operation: Operation, original: Method): Method {
    const instrumentation = this;
    return function (this: unknown, ...args: unknown[]) {
      const parent = context.active();
      // Bound v2 methods can outlive unpatch. Respect disable even for those.
      if (!instrumentation.isEnabled() || isTracingSuppressed(parent) || parent.getValue(IN_COHERE_CALL)) {
        return original.apply(this, args);
      }
      const config = generateTraceConfig(instrumentation._traceConfig);
      let span: Span;
      try {
        const tracer = new FITracer({ tracer: instrumentation.tracer, traceConfig: config });
        const name = operation === "chatStream" ? "Chat Stream" : operation[0].toUpperCase() + operation.slice(1);
        span = tracer.startSpan(`Cohere ${name}`, { kind: SpanKind.INTERNAL, attributes: inputAttributes(operation, object(args[0]), config) }, parent);
      } catch {
        diag.warn("Cohere instrumentation could not start span");
        return original.apply(this, args);
      }
      const execContext = trace.setSpan(parent, span).setValue(IN_COHERE_CALL, true);
      let ended = false;
      const finish = (response: Data, error?: unknown, cancelled = false) => {
        if (ended) return;
        ended = true;
        try {
          span.setAttributes(outputAttributes(operation, response, config));
          if (error !== undefined) {
            span.recordException(error instanceof Error ? error : new Error(String(error)));
            span.setStatus({ code: SpanStatusCode.ERROR, message: error instanceof Error ? error.message : String(error) });
          } else if (cancelled) {
            span.setAttribute("cohere.stream.cancelled", true);
          } else {
            span.setStatus({ code: SpanStatusCode.OK });
          }
        } catch {
          diag.warn("Cohere instrumentation could not record response");
        } finally {
          span.end();
        }
      };
      const streams = new WeakMap<object, object>();
      const resolved = (result: unknown): unknown => {
        if (operation === "chatStream" && result && typeof (result as AsyncIterable<unknown>)[Symbol.asyncIterator] === "function") {
          if (!streams.has(result as object)) streams.set(result as object, wrapStream(result as object, execContext, !hideOutput(config), finish));
          return streams.get(result as object);
        }
        finish(object(result));
        return result;
      };
      const rejected = (error: unknown): never => {
        finish({}, error ?? new Error("Cohere request failed"));
        throw error;
      };
      let pending: unknown;
      try {
        pending = context.with(execContext, () => original.apply(this, args));
      } catch (error) { return rejected(error); }
      return observePromise(pending, resolved, rejected);
    };
  }
}

/** Keep SDK promise helpers (notably HttpResponsePromise.withRawResponse). */
function observePromise(pending: unknown, resolved: (value: unknown) => unknown, rejected: (error: unknown) => never): unknown {
  const promise = pending as Promise<unknown> & { withRawResponse?: () => Promise<{ data: unknown; rawResponse: unknown }> };
  const observed = promise.then(resolved, rejected);
  if (typeof promise.withRawResponse !== "function") return observed;
  // A caller may exclusively use withRawResponse. Do not leave an unobserved
  // rejected branch merely because telemetry also observes the normal promise.
  void observed.catch(() => {});
  return new Proxy(promise, {
    get(target, key) {
      if (key === "then") return observed.then.bind(observed);
      if (key === "catch") return observed.catch.bind(observed);
      if (key === "finally") return observed.finally.bind(observed);
      if (key === "withRawResponse") return () => target.withRawResponse!().then(
        (raw) => ({ ...raw, data: resolved(raw.data) }), rejected,
      );
      const value = Reflect.get(target, key, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}
