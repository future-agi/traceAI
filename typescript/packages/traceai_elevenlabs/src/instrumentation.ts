import {
  Attributes,
  context,
  SpanKind,
  trace,
  TracerProvider,
} from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import {
  InstrumentationBase,
  InstrumentationConfig,
} from "@opentelemetry/instrumentation";
import {
  FITracer,
  generateTraceConfig,
  getAttributesFromContext,
  TraceConfigOptions,
} from "@traceai/fi-core";
import {
  SemanticConventions as S,
  FISpanKind,
} from "@traceai/fi-semantic-conventions";
import {
  diagnose,
  identifier,
  Operation,
  record,
  safely,
  TEXT_LIMIT,
} from "./operation.js";
import { observeResponse } from "./responses.js";
import { VERSION } from "./version.js";

const NAME = "@traceai/elevenlabs";
const methods: Record<string, readonly string[]> = {
  textToSpeech: [
    "convert",
    "stream",
    "convertWithTimestamps",
    "streamWithTimestamps",
  ],
  speechToText: ["convert"],
};
// Weak ownership prevents duplicate semantic spans without retaining customer clients.
const wrapped = new WeakMap<
  object,
  { owner: ElevenLabsInstrumentation; proxy: object }
>();
const originals = new WeakMap<object, object>();
const moduleOwners = new WeakMap<object, ElevenLabsInstrumentation>();
type Patch = { original?: PropertyDescriptor; getter: () => unknown };

type SDKModule = { ElevenLabsClient: { prototype: object } };

export interface ElevenLabsInstrumentationConfig {
  instrumentationConfig?: InstrumentationConfig;
  tracerProvider?: TracerProvider;
  traceConfig?: TraceConfigOptions;
  /** Absolute span lifetime, including request and consumption. Default 300000 ms. */
  maxSpanDurationMs?: number;
  /** Bound active telemetry, not SDK calls. Excess calls pass through. Default 1024. */
  maxActiveSpans?: number;
}

function bounded(
  value: number | undefined,
  fallback: number,
  maximum: number,
): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 1
    ? Math.min(Math.floor(value), maximum)
    : fallback;
}

/**
 * Explicit client wrapping works with both ESM and CommonJS, including SDKs
 * imported before instrumentation. This package does not install import hooks.
 */
export class ElevenLabsInstrumentation extends InstrumentationBase {
  private readonly options: ElevenLabsInstrumentationConfig;
  private readonly active = new Set<Operation>();
  private readonly modules = new Map<object, Map<string, Patch>>();

  constructor(options: ElevenLabsInstrumentationConfig = {}) {
    super(NAME, VERSION, options.instrumentationConfig ?? {});
    this.options = options;
    if (options.tracerProvider) this.setTracerProvider(options.tracerProvider);
  }

  protected init() {
    return [];
  }

  /** Return a typed proxy; the original client and its namespaces are not mutated. */
  wrapClient<T extends object>(client: T): T {
    client = (originals.get(client) ?? client) as T;
    const existing = wrapped.get(client);
    if (existing && (existing.owner === this || existing.owner.isEnabled()))
      return existing.proxy as T;
    const proxy = new Proxy(client, {
      get: (target, key) => {
        const value = Reflect.get(target, key, target);
        if (
          typeof key === "string" &&
          Object.hasOwn(methods, key) &&
          value &&
          typeof value === "object"
        )
          return this.wrapNamespace(value, key);
        return value;
      },
    });
    wrapped.set(client, { owner: this, proxy });
    originals.set(proxy, client);
    return proxy;
  }

  /**
   * Optional module registration for all clients from this SDK module. Pass the
   * ESM namespace (`import * as sdk`) or the CommonJS module. No import order rule.
   */
  manuallyInstrument(module: SDKModule): void {
    if (!this.isEnabled()) return;
    const prototype = module.ElevenLabsClient.prototype;
    if (moduleOwners.has(prototype)) return;
    const saved = new Map<string, Patch>();
    for (const namespace of Object.keys(methods)) {
      const original = Object.getOwnPropertyDescriptor(prototype, namespace);
      let current: object | null = prototype;
      let descriptor = original;
      while (!descriptor && (current = Object.getPrototypeOf(current)))
        descriptor = Object.getOwnPropertyDescriptor(current, namespace);
      if (!descriptor?.get || (original && !original.configurable)) continue;
      const getter = descriptor.get;
      const wrap = (value: object) =>
        this.isEnabled() ? this.wrapNamespace(value, namespace) : value;
      const patched = function (this: unknown) {
        const value = getter.call(this);
        return wrap(value);
      };
      Object.defineProperty(prototype, namespace, {
        ...descriptor,
        configurable: true,
        get: patched,
      });
      saved.set(namespace, { original, getter: patched });
    }
    this.modules.set(prototype, saved);
    moduleOwners.set(prototype, this);
  }

  /** Restore module getters. Existing client proxies then pass through untraced. */
  unpatch(): void {
    this.disable();
  }

  override disable(): void {
    super.disable();
    // Base constructors may call disable before subclass fields are initialized.
    if (this.modules) {
      for (const [prototype, descriptors] of this.modules) {
        for (const [key, patch] of descriptors)
          safely(() => {
            if (
              Object.getOwnPropertyDescriptor(prototype, key)?.get !==
              patch.getter
            )
              return;
            if (patch.original)
              Object.defineProperty(prototype, key, patch.original);
            else Reflect.deleteProperty(prototype, key);
          });
        moduleOwners.delete(prototype);
      }
      this.modules.clear();
    }
    this.closePendingSpans();
  }

  /** Close unfinished telemetry without reading/cancelling customer streams. */
  closePendingSpans(): void {
    if (this.active)
      for (const operation of this.active) operation.end("incomplete");
  }

  /** Stop tracing before shutting down your OpenTelemetry provider. */
  shutdown(): void {
    this.disable();
  }

  private wrapNamespace<T extends object>(namespace: T, name: string): T {
    namespace = (originals.get(namespace) ?? namespace) as T;
    const existing = wrapped.get(namespace);
    if (existing && (existing.owner === this || existing.owner.isEnabled()))
      return existing.proxy as T;
    const functions = new Map<
      PropertyKey,
      { original: unknown; wrapper: unknown }
    >();
    const proxy = new Proxy(namespace, {
      get: (target, key) => {
        const value = Reflect.get(target, key, target);
        if (
          typeof key !== "string" ||
          !methods[name].includes(key) ||
          typeof value !== "function"
        )
          return value;
        const cached = functions.get(key);
        if (cached?.original === value) return cached.wrapper;
        const wrapper = new Proxy(value, {
          apply: (fn, receiver, args) => {
            return this.invoke(
              fn as (...args: unknown[]) => unknown,
              originals.get(receiver) ?? receiver,
              args,
              `${name}.${key}`,
            );
          },
        });
        functions.set(key, { original: value, wrapper });
        return wrapper;
      },
    });
    wrapped.set(namespace, { owner: this, proxy });
    originals.set(proxy, namespace);
    return proxy;
  }

  private invoke(
    fn: (...args: unknown[]) => unknown,
    receiver: unknown,
    args: unknown[],
    method: string,
  ): unknown {
    let operation: Operation | undefined;
    let callContext = context.active();
    try {
      const stt = method === "speechToText.convert";
      const request = record(args[stt ? 0 : 1]);
      if (
        this.isEnabled() &&
        !isTracingSuppressed(callContext) &&
        request.webhook !== true &&
        this.active.size < bounded(this.options.maxActiveSpans, 1024, 100_000)
      ) {
        const policy = generateTraceConfig(this.options.traceConfig);
        const attributes: Attributes = {
          ...getAttributesFromContext(callContext),
          [S.FI_SPAN_KIND]: FISpanKind.LLM,
          [S.LLM_PROVIDER]: "elevenlabs",
          "gen_ai.operation.name": stt ? "transcription" : "speech",
          "elevenlabs.operation": method,
        };
        const model = identifier(request.modelId);
        if (model) attributes[S.LLM_MODEL_NAME] = model;
        const voice = !stt && identifier(args[0]);
        if (voice) attributes["elevenlabs.voice.id"] = voice;
        const format = identifier(request.outputFormat);
        if (format) attributes["elevenlabs.output_format"] = format;
        if (
          !stt &&
          typeof request.text === "string" &&
          !policy.hideInputs &&
          !policy.hideInputMessages &&
          !policy.hideInputText
        )
          attributes[S.INPUT_VALUE] = request.text.slice(0, TEXT_LIMIT);
        const tracer = new FITracer({
          tracer: this.tracer,
          traceConfig: policy,
        });
        const span = tracer.startSpan(
          `ElevenLabs ${method}`,
          { kind: SpanKind.CLIENT, attributes },
          callContext,
        );
        operation = new Operation(
          span,
          policy,
          bounded(this.options.maxSpanDurationMs, 300_000, 86_400_000),
          (ended) => this.active.delete(ended),
        );
        this.active.add(operation);
        operation.watchAbort(record(args[stt ? 1 : 2]).abortSignal);
        callContext = trace.setSpan(callContext, span);
      }
    } catch {
      diagnose();
      operation?.end("incomplete");
      operation = undefined;
    }
    let response;
    try {
      response = context.with(callContext, () =>
        Reflect.apply(fn, receiver, args),
      );
    } catch (error) {
      operation?.error(error);
      throw error;
    }
    if (!operation) return response;
    try {
      return observeResponse(
        response as Parameters<typeof observeResponse>[0],
        operation,
        method,
      );
    } catch {
      diagnose();
      operation.end("incomplete");
      return response;
    }
  }
}
