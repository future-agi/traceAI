import { context, diag, trace, Attributes, Span, SpanStatusCode, Tracer, INVALID_SPAN_CONTEXT } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { InstrumentationBase, InstrumentationNodeModuleDefinition } from "@opentelemetry/instrumentation";
import { types } from "node:util";
import { FITracer, TraceConfigOptions } from "@traceai/fi-core";
import { safelyGetFIAttributes } from "./utils";
import { VERSION } from "./version";
import { createTelemetryIntegration, TelemetryIntegration } from "./telemetry";
import { observeStreamResult } from "./streams";

const METHODS = ["generateText", "streamText", "generateObject", "streamObject", "embed", "embedMany"] as const;
const WRAPPED = Symbol.for("@traceai/vercel.wrapped");
const STATE = Symbol.for("@traceai/vercel.auto");
type SDKModule = Record<string, any>;
type Globals = typeof globalThis & {
  AI_SDK_TELEMETRY_INTEGRATIONS?: TelemetryIntegration[];
  [STATE]?: VercelAIInstrumentation;
};

/** Only our bookkeeping is guarded. Never retry an application call after an error. */
function safely<T>(fn: () => T): T | undefined {
  try { return fn(); } catch {
    try { diag.debug("@traceai/vercel: telemetry unavailable"); } catch { /* Diagnostics must also fail open. */ }
    return undefined;
  }
}

function nativeAttributes(attributes: Attributes): Attributes {
  const mapped = safelyGetFIAttributes(attributes) ?? {};
  const result: Attributes = { ...mapped, "gen_ai.span.kind": mapped["fi.span.kind"], "traceai.capture.layer": "library" };
  // Native content aliases (especially embeddings) must not bypass canonical privacy rules.
  for (const [key, value] of Object.entries(attributes)) {
    if (!key.startsWith("ai.")) result[key] = value;
  }
  if (typeof attributes["ai.model.provider"] === "string") result["gen_ai.provider.name"] = attributes["ai.model.provider"];
  else if (typeof attributes["gen_ai.system"] === "string") result["gen_ai.provider.name"] = attributes["gen_ai.system"];
  const inputTokens = result["gen_ai.usage.input_tokens"];
  const outputTokens = result["gen_ai.usage.output_tokens"];
  // Derive the total only when the span did not set a canonical total itself.
  if (typeof inputTokens === "number" && typeof outputTokens === "number" && typeof attributes["gen_ai.usage.total_tokens"] !== "number") {
    result["gen_ai.usage.total_tokens"] = inputTokens + outputTokens;
  }
  // Tool arguments are already normalized to input.value. The legacy alias is
  // outside both FI masking and the export boundary's covered content families.
  delete result["tool.parameters"];
  delete result["gen_ai.embeddings"];
  for (const key of Object.keys(result)) {
    if (/embedding.*vector/.test(key)) {
      if (Array.isArray(result[key])) result["gen_ai.embeddings.dimension.count"] = (result[key] as unknown[]).length;
      delete result[key];
    } else if (result[key] === undefined) delete result[key];
  }
  return result;
}

function nativeSpan(span: Span, initial: Attributes, pending: Set<Span>): Span {
  const attributes = { ...initial };
  let ended = false;
  let proxy: Span;
  proxy = new Proxy(span, {
    get(target, key) {
      if (key === "end") return (...args: any[]) => {
        if (ended) return;
        ended = true;
        pending.delete(proxy);
        safely(() => Reflect.apply(target.end, target, args));
      };
      if (key === "setAttributes" || key === "setAttribute") return (...args: any[]) => {
        safely(() => {
          Object.assign(attributes, key === "setAttribute" ? { [args[0]]: args[1] } : args[0]);
          target.setAttributes(nativeAttributes(attributes));
        });
        return proxy;
      };
      const value = Reflect.get(target, key, target);
      if (typeof value !== "function") return value;
      return (...args: any[]) => {
        const result = safely(() => Reflect.apply(value, target, args));
        return result === target ? proxy : result;
      };
    },
  });
  pending.add(proxy);
  return proxy;
}

/** Adapts SDK-native v5/v6 spans while leaving the public return object intact. */
function nativeTracer(tracer: Tracer, pending: Set<Span>): Tracer {
  const noop = () => trace.wrapSpanContext(INVALID_SPAN_CONTEXT);
  return {
    startSpan(name, options, ctx) {
      return safely(() => nativeSpan(tracer.startSpan(name, {
        ...options, attributes: nativeAttributes(options?.attributes ?? {}),
      }, ctx), options?.attributes ?? {}, pending)) ?? noop();
    },
    startActiveSpan(name: string, ...args: any[]): any {
      const fn = args[args.length - 1];
      const options = args.length > 1 ? args[0] : {};
      const attributes = options?.attributes ?? {};
      let entered = false;
      const callback = (span: Span) => {
        entered = true;
        return fn(nativeSpan(span, attributes, pending));
      };
      const callArgs = args.length === 1 ? [callback] : [
        { ...options, attributes: nativeAttributes(attributes) },
        ...args.slice(1, -1), callback,
      ];
      try { return Reflect.apply(tracer.startActiveSpan, tracer, [name, ...callArgs]); }
      catch (error) {
        if (entered) throw error;
        return callback(noop());
      }
    },
  } as Tracer;
}

/** Node module hooks; initialize before importing `ai` (ESM also needs the OTel loader). */
export class VercelAIInstrumentation extends InstrumentationBase {
  readonly integration: TelemetryIntegration;
  private readonly traceConfig?: TraceConfigOptions;
  private readonly tracers = new WeakMap<Tracer, Tracer>();

  private fiTracer(tracer: Tracer = this.tracer): Tracer {
    let wrapped = this.tracers.get(tracer);
    if (!wrapped) {
      wrapped = new FITracer({ tracer, traceConfig: this.traceConfig });
      this.tracers.set(tracer, wrapped);
    }
    return wrapped;
  }

  constructor({ instrumentationConfig = {}, traceConfig }: {
    instrumentationConfig?: { enabled?: boolean };
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/vercel", VERSION, { ...instrumentationConfig, enabled: false });
    this.traceConfig = traceConfig;
    this.integration = createTelemetryIntegration(() => this.fiTracer());
    if (instrumentationConfig.enabled !== false) this.enable();
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition("ai", ["^5.0.0", "^6.0.0", "^7.0.0"],
      (module: SDKModule, version?: string) => this.manuallyInstrument(module, version));
  }

  override enable(): void {
    super.enable();
    // This is the public v7 registry. It works even when no module loader is installed.
    const globals = globalThis as Globals;
    safely(() => {
      const integrations = globals.AI_SDK_TELEMETRY_INTEGRATIONS ??= [];
      if (!integrations.includes(this.integration)) integrations.push(this.integration);
    });
  }

  override disable(): void {
    super.disable();
    safely(() => {
      const globals = globalThis as Globals;
      const integrations = globals.AI_SDK_TELEMETRY_INTEGRATIONS;
      const index = integrations?.indexOf(this.integration) ?? -1;
      if (index >= 0) integrations!.splice(index, 1);
    });
  }

  /** The returned namespace is significant: v5/v6 CJS exports are non-configurable getters. */
  manuallyInstrument(module: SDKModule, version = "6.0.0"): SDKModule {
    if (!/^[567]\./.test(version)) return module;
    return safely(() => {
      const target = types.isProxy(module) ? module : Object.assign({}, module);
      const v7 = version.startsWith("7.");
      for (const name of METHODS) {
        if (typeof target[name] !== "function" || target[name][WRAPPED]) continue;
        const wrap = (original: (...args: any[]) => any) => {
          const instrumentation = this;
          const wrapped = function(this: unknown, ...args: any[]) {
            let pending: Set<Span> | undefined;
            let dispatcher: TelemetryIntegration | undefined;
            let options = args[0];
            if (instrumentation.isEnabled() && options != null && typeof options === "object" && !isTracingSuppressed(context.active())) {
              options = safely(() => {
                const telemetry = v7 ? options.telemetry ?? options.experimental_telemetry : options.experimental_telemetry;
                if (telemetry?.isEnabled === false) return options;
                if (v7) {
                  // Preserve the SDK's local-over-global precedence and callback receivers.
                  const configured = telemetry?.integrations ?? (globalThis as Globals).AI_SDK_TELEMETRY_INTEGRATIONS ?? [];
                  const integrations = Array.isArray(configured) ? configured : [configured];
                  const integration = createTelemetryIntegration(() => instrumentation.fiTracer(), `ai.${name}`);
                  dispatcher = integration;
                  return { ...options, telemetry: { ...telemetry,
                    integrations: [...integrations.filter(item => item !== instrumentation.integration), integration],
                  } };
                }
                pending = new Set<Span>();
                return { ...options, experimental_telemetry: {
                  ...telemetry, isEnabled: true,
                  tracer: nativeTracer(instrumentation.fiTracer(telemetry?.tracer ?? instrumentation.tracer), pending),
                } };
              }) ?? args[0];
            }
            // Not async: stream result helpers, promises, exceptions and identities survive.
            const execute = () => Reflect.apply(original, this, args.length ? [options, ...args.slice(1)] : args);
            const result = dispatcher ? dispatcher.withOperationContext(execute) : execute();
            if ((pending || dispatcher) && name.startsWith("stream")) {
              const finish = (error?: unknown) => {
                const end = Date.now();
                dispatcher?.finishStream(error);
                for (const span of [...(pending ?? [])]) safely(() => {
                  if (error !== undefined) {
                    span.setStatus({ code: SpanStatusCode.ERROR });
                    if (error instanceof Error) span.recordException(error);
                  } else span.setAttribute("traceai.wire.incomplete", true);
                  span.end(end);
                });
              };
              safely(() => observeStreamResult(result, error => finish(error), () => finish()));
            }
            return result;
          };
          Object.defineProperty(wrapped, WRAPPED, { value: true });
          return wrapped;
        };
        const original = target[name];
        if (original.__wrapped) {
          // OTel's _wrap first unwraps an existing shimmer wrapper. Preserve it
          // by wrapping a fresh delegate instead; never invoke its __unwrap.
          const holder = { [name]: function(this: unknown, ...args: any[]) { return Reflect.apply(original, this, args); } };
          this._wrap(holder, name, wrap);
          Object.defineProperty(target, name, { value: holder[name] });
        } else this._wrap(target, name, wrap);
      }
      return target;
    }) ?? module;
  }
}

/** Once per process; no provider replacement, network access, signal handlers or teardown. */
export function enableVercelInstrumentation(): VercelAIInstrumentation | undefined {
  const globals = globalThis as Globals;
  if (globals[STATE]) return globals[STATE];
  return safely(() => {
    const instrumentation = new VercelAIInstrumentation({ instrumentationConfig: { enabled: false } });
    instrumentation.enable();
    globals[STATE] = instrumentation;
    return instrumentation;
  });
}

export { enableVercelInstrumentation as enable };
