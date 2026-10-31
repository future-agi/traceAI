import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
  isWrapped,
} from "@opentelemetry/instrumentation";
import { context, trace, SpanKind, SpanStatusCode } from "@opentelemetry/api";
import { FITracer, generateTraceConfig, TraceConfigOptions } from "@traceai/fi-core";
import { queryAttributes } from "./queryAttributes";

const VERSION = "0.1.1"; // x-release-please-version

type QueryFunction = (this: any, ...args: any[]) => any;
type QueryPrototype = { query: QueryFunction };
type Patch = { original: QueryFunction; wrapper: QueryFunction };
const owners = new WeakMap<QueryPrototype, PgVectorInstrumentation>();
const patched = new Set<QueryPrototype>();

function canAssign(object: object, key: string): boolean {
  for (let target: object | null = object; target; target = Object.getPrototypeOf(target)) {
    const descriptor = Object.getOwnPropertyDescriptor(target, key);
    if (descriptor) {
      if (!("value" in descriptor)) return typeof descriptor.set === "function";
      return !!descriptor.writable && (target === object || Object.isExtensible(object));
    }
  }
  return Object.isExtensible(object);
}

export function isPatched(): boolean {
  return patched.size > 0;
}

export interface PgVectorInstrumentationConfig extends InstrumentationConfig {
  /** Capture SQL (up to 1000 characters). Off by default; hideInputs/hideInputText override it. */
  captureQueries?: boolean;
  /** @deprecated Alias for captureQueries, retained for older README examples. */
  captureStatements?: boolean;
  /** @deprecated Raw parameter capture is not supported. This option has no effect. */
  captureParameters?: boolean;
  /** @deprecated Vector contents are never captured. This option has no effect. */
  captureQueryVectors?: boolean;
  /** @deprecated Vector contents are never captured. This option has no effect. */
  captureResultVectors?: boolean;
}

export class PgVectorInstrumentation extends InstrumentationBase<PgVectorInstrumentationConfig> {
  private _traceConfig?: TraceConfigOptions;
  // Lazily initialized: InstrumentationBase can call enable/patch in its constructor.
  private _patches?: Map<QueryPrototype, Patch>;
  private _manualModules?: Set<any>;

  constructor({ instrumentationConfig, traceConfig }: {
    instrumentationConfig?: PgVectorInstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/pgvector", VERSION, { ...instrumentationConfig });
    this._traceConfig = traceConfig;
  }

  public override getConfig(): PgVectorInstrumentationConfig {
    return super.getConfig();
  }

  public override setConfig(config: PgVectorInstrumentationConfig): void {
    super.setConfig(config);
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "pg", [">=8.0.0 <9"], this.patch.bind(this), this.unpatch.bind(this)
    );
  }

  public override enable(): void {
    super.enable();
    this._manualModules?.forEach((module) => this.patch(module));
  }

  public override disable(): void {
    super.disable();
    this._patches?.forEach((_, prototype) => this.restore(prototype));
  }

  public manuallyInstrument(pgModule: any): void {
    (this._manualModules ??= new Set()).add(pgModule);
    if (this.isEnabled()) this.patch(pgModule);
  }

  private patch(moduleExports: any) {
    const prototype = (moduleExports.Client ?? moduleExports.default?.Client)?.prototype as QueryPrototype | undefined;
    if (!prototype || typeof prototype.query !== "function" || owners.has(prototype)) return moduleExports;
    // Do not stack with a separately enabled OTel pg instrumentor.
    if (isWrapped(prototype.query)) {
      this._diag.warn("pg.Client.query is already instrumented; use only one pg instrumentation.");
      return moduleExports;
    }
    const original = prototype.query;
    const instrumentation = this;
    const wrapper: QueryFunction = function (...args) {
      if (!instrumentation.isEnabled()) return original.apply(this, args);
      return instrumentation.wrapQuery(this, original, args);
    };
    prototype.query = wrapper;
    (this._patches ??= new Map()).set(prototype, { original, wrapper });
    owners.set(prototype, this);
    patched.add(prototype);
    // Pool.query delegates to Client.query. Wrapping it too duplicates execution spans.
    return moduleExports;
  }

  private unpatch(moduleExports: any) {
    this.restore((moduleExports.Client ?? moduleExports.default?.Client)?.prototype);
    return moduleExports;
  }

  private restore(prototype?: QueryPrototype): void {
    if (!prototype) return;
    const patch = this._patches?.get(prototype);
    if (!patch) return;
    // Do not overwrite a wrapper installed by somebody else after ours.
    if (prototype.query === patch.wrapper) prototype.query = patch.original;
    this._patches!.delete(prototype);
    owners.delete(prototype);
    patched.delete(prototype);
  }

  private wrapQuery(receiver: any, original: QueryFunction, args: any[]): any {
    const query = args[0];
    const submittable = query != null && typeof query.submit === "function";
    // pg gives an existing Submittable callback precedence over the second argument.
    const callbackIndex = submittable
      ? (typeof query.callback !== "function" && typeof args[1] === "function" ? 1 : -1)
      : (typeof args[2] === "function" ? 2 : typeof args[1] === "function" ? 1 : -1);
    const callback = submittable && typeof query.callback === "function" ? query.callback
      : callbackIndex >= 0 ? args[callbackIndex] : query?.callback;
    const hasCallback = typeof callback === "function";
    // A custom Submittable without a callback or EventEmitter has no observable
    // completion contract. Pass it through rather than invent an early success.
    if (submittable && ((!hasCallback && typeof query.emit !== "function") || !canAssign(query, hasCallback ? "callback" : "emit"))) {
      return original.apply(receiver, args);
    }

    const text = typeof query === "string" ? query : typeof query?.text === "string" ? query.text : "";
    const values = Array.isArray(args[1]) ? args[1] : query?.values;
    const { name, attributes } = queryAttributes(text, values);
    const traceConfig = generateTraceConfig(this._traceConfig);
    const config = this.getConfig();
    if ((config.captureQueries ?? config.captureStatements) && !traceConfig.hideInputs && !traceConfig.hideInputText && text) {
      attributes["db.statement"] = text.length > 1000 ? text.slice(0, 1000) + "..." : text;
    }
    // Resolve the current tracer/provider and privacy policy, including env settings.
    const tracer = new FITracer({ tracer: this.tracer, traceConfig });
    const parent = context.active();
    const span = tracer.startSpan(name, { kind: SpanKind.CLIENT, attributes }, parent);
    let ended = false;
    let restore = () => {};
    const finish = (error?: any, result?: any) => {
      if (ended) return;
      ended = true;
      restore();
      if (error) {
        span.recordException(error);
        span.setStatus({ code: SpanStatusCode.ERROR, message: error?.message });
      } else {
        if (Array.isArray(result?.rows)) span.setAttribute("db.response.returned_rows", result.rows.length);
        if (typeof result?.rowCount === "number") span.setAttribute("db.response.affected_rows", result.rowCount);
        span.setStatus({ code: SpanStatusCode.OK });
      }
      span.end();
    };

    if (hasCallback) {
      const wrapped: QueryFunction = function (...callbackArgs) {
        finish(callbackArgs[0], callbackArgs[1]);
        // Complete telemetry before user code; a callback throw is not a DB failure.
        return context.with(parent, () => callback.apply(this, callbackArgs));
      };
      if (submittable) {
        const descriptor = Object.getOwnPropertyDescriptor(query, "callback");
        query.callback = wrapped;
        restore = () => {
          if (query.callback === wrapped) {
            if (callbackIndex === 1) query.callback = callback;
            else if (descriptor) Object.defineProperty(query, "callback", descriptor);
            else delete query.callback;
          }
        };
      } else if (callbackIndex >= 0) {
        args[callbackIndex] = wrapped;
      } else {
        // Do not mutate reusable or frozen query config objects.
        args[0] = Object.create(Object.getPrototypeOf(query), {
          ...Object.getOwnPropertyDescriptors(query),
          callback: { value: wrapped, writable: true, enumerable: true, configurable: true },
        });
      }
    } else if (submittable) {
      const originalEmit = query.emit;
      const descriptor = Object.getOwnPropertyDescriptor(query, "emit");
      const wrappedEmit: QueryFunction = function (event, ...eventArgs) {
        if (event === "end") finish(undefined, eventArgs[0]);
        else if (event === "error") finish(eventArgs[0]);
        // Unlike adding an error listener, this preserves unhandled-error throws.
        return context.with(parent, () => originalEmit.call(this, event, ...eventArgs));
      };
      query.emit = wrappedEmit;
      restore = () => {
        if (query.emit === wrappedEmit) {
          if (descriptor) Object.defineProperty(query, "emit", descriptor);
          else delete query.emit;
        }
      };
    }

    try {
      const result = context.with(trace.setSpan(parent, span), () => original.apply(receiver, args));
      if (!hasCallback && !submittable && result && typeof result.then === "function") {
        return result.then(
          (value: any) => { finish(undefined, value); return value; },
          (error: any) => { finish(error); throw error; }
        );
      }
      return result;
    } catch (error) {
      finish(error);
      throw error;
    }
  }
}

export default PgVectorInstrumentation;
