import { context, diag, trace, SpanKind, SpanStatusCode, type Span, type Tracer } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { FITracer, generateTraceConfig, type TraceConfigOptions, type TraceConfig } from "@traceai/fi-core";
import { SemanticConventions as SC, FISpanKind } from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

export { VERSION } from "./version";

export interface TransformersInstrumentationOptions {
  traceConfig?: TraceConfigOptions;
  /** Defaults to the global OpenTelemetry tracer. Configure its provider first. */
  tracer?: Tracer;
  enabled?: boolean;
}

const TASKS = new Set(["text-generation", "text2text-generation", "summarization", "feature-extraction", "question-answering"]);
// Wrapping an already instrumented value never creates another span, even across instances.
const wrapped = new WeakSet<object>();
const MAX_BYTES = 8192;
const CONTENT_KEYS = ["role", "content", "generated_text", "summary_text", "answer", "score", "start", "end", "question", "context"];
const GENERATION_KEYS = ["max_new_tokens", "min_new_tokens", "temperature", "top_p", "top_k", "repetition_penalty", "do_sample", "num_beams", "return_full_text"];

function safely<T>(fn: () => T): T | undefined {
  try { return fn(); } catch {
    // Diagnostics must not bypass FI privacy by printing a customer's exception or payload.
    try { diag.debug("@traceai/huggingface-transformers: telemetry operation skipped; inference continues"); } catch { /* A failing logger must also fail open. */ }
    return undefined;
  }
}

/** Only data properties are inspected: never run customer getters or toJSON. */
function own(value: unknown, key: string): unknown {
  if ((typeof value !== "object" && typeof value !== "function") || value === null) return undefined;
  return Object.getOwnPropertyDescriptor(value, key)?.value;
}

/** Bounded, allowlisted copy. Model graphs, callbacks, options and Tensor data are not visited. */
function contentJSON(value: unknown): string | undefined {
  let remaining = MAX_BYTES;
  let nodes = 256;
  function copy(item: unknown, depth: number): unknown {
    if (--nodes < 0 || depth > 8) throw new Error("content limit");
    if (typeof item === "string") {
      if (item.length > remaining) throw new Error("content limit");
      remaining -= Buffer.byteLength(item, "utf8");
      if (remaining < 0) throw new Error("content limit");
      return item;
    }
    if (item === null || typeof item === "boolean") return item;
    if (typeof item === "number") return Number.isFinite(item) ? item : undefined;
    if (Array.isArray(item)) {
      if (item.length > 128) throw new Error("content limit");
      return Array.from({ length: item.length }, (_, i) => copy(own(item, String(i)), depth + 1));
    }
    if (item && typeof item === "object" && (Object.getPrototypeOf(item) === Object.prototype || Object.getPrototypeOf(item) === null)) {
      const result: Record<string, unknown> = {};
      for (const key of CONTENT_KEYS) {
        const v = own(item, key);
        if (v !== undefined) result[key] = copy(v, depth + 1);
      }
      return result;
    }
    return undefined;
  }
  const result = JSON.stringify(copy(value, 0));
  return result !== undefined && Buffer.byteLength(result, "utf8") <= MAX_BYTES ? result : undefined;
}

function capture(span: Span, key: string, value: unknown): void {
  safely(() => {
    const json = contentJSON(value);
    if (json !== undefined) span.setAttribute(key, json);
  });
}

/**
 * Explicit Node.js CPU pipeline instrumentation. No global patching and no model-download spans.
 * Wrap the module's pipeline factory, or wrap a directly constructed callable pipeline.
 */
export class TransformersInstrumentation {
  private readonly tracer: FITracer;
  private readonly config: TraceConfig;
  private readonly cache = new WeakMap<object, object>();
  private readonly models = new WeakMap<object, string>();
  private enabled: boolean;

  constructor(options: TransformersInstrumentationOptions = {}) {
    this.config = generateTraceConfig(options.traceConfig);
    this.tracer = new FITracer({
      tracer: options.tracer ?? trace.getTracer("@traceai/huggingface-transformers", VERSION),
      traceConfig: this.config,
    });
    this.enabled = options.enabled ?? true;
  }

  enable(): void { this.enabled = true; }
  disable(): void { this.enabled = false; }

  /** Returns a facade rather than mutating immutable ESM namespace exports. */
  wrapModule<T>(module: T): T {
    if (!module || (typeof module !== "object" && typeof module !== "function")) return module;
    if (wrapped.has(module)) return module;
    const cached = this.cache.get(module);
    if (cached) return cached as T;
    const facade = safely(() => {
      const factory = Reflect.get(module, "pipeline");
      if (typeof factory !== "function") return undefined;
      const instrumentor = this;
      const pipeline = new Proxy(factory, {
        apply(target, thisArg, args) {
          const result = Reflect.apply(target, thisArg === facade ? module : thisArg, args);
          const wrapResult = (value: unknown) => {
            if (typeof value === "function" && typeof args[1] === "string") instrumentor.models.set(value, args[1]);
            return instrumentor.wrapPipeline(value);
          };
          return result instanceof Promise ? result.then(wrapResult) : wrapResult(result);
        },
      });
      const descriptors: PropertyDescriptorMap = Object.getOwnPropertyDescriptors(module);
      descriptors.pipeline = { value: pipeline, enumerable: true, configurable: true, writable: true };
      return Object.defineProperties(Object.create(Object.getPrototypeOf(module)), descriptors) as object;
    });
    if (!facade) return module;
    this.cache.set(module, facade);
    wrapped.add(facade);
    return facade as T;
  }

  /** Preserves the pipeline's overloads, properties, methods and returned object identities. */
  wrapPipeline<T>(pipeline: T): T {
    if (typeof pipeline !== "function" || wrapped.has(pipeline)) return pipeline;
    const cached = this.cache.get(pipeline);
    if (cached) return cached as T;
    const task = safely(() => Reflect.get(pipeline, "task"));
    if (typeof task !== "string" || !TASKS.has(task)) return pipeline;
    const proxy = new Proxy(pipeline, {
      apply: (target, thisArg, args) => this.invoke(target, thisArg, args, task),
    });
    this.cache.set(pipeline, proxy);
    wrapped.add(proxy);
    return proxy as T;
  }

  private invoke(target: Function, thisArg: unknown, args: unknown[], task: string): unknown {
    const parent = context.active();
    if (!this.enabled || isTracingSuppressed(parent)) return Reflect.apply(target, thisArg, args);
    const embedding = task === "feature-extraction";
    const qa = task === "question-answering";
    const span = safely(() => this.tracer.startSpan(`transformers.${task}`, {
      kind: SpanKind.INTERNAL,
      attributes: {
        [SC.FI_SPAN_KIND]: embedding ? FISpanKind.EMBEDDING : qa ? FISpanKind.CHAIN : FISpanKind.LLM,
        [SC.LLM_PROVIDER]: "huggingface",
        "transformers.task": task,
      },
    }, parent));
    // Tracing failures must not prevent inference or execute it twice.
    if (!span) return Reflect.apply(target, thisArg, args);
    safely(() => {
      const model = this.models.get(target) ?? own(own(own(target, "model"), "config"), "_name_or_path");
      if (typeof model === "string" && model.length <= 512) {
        span.setAttribute(embedding ? SC.EMBEDDING_MODEL_NAME : qa ? "transformers.model" : SC.LLM_MODEL_NAME, model);
      }
      if (!embedding && !qa) {
        const options = args[1];
        const params: Record<string, number | boolean> = {};
        for (const key of GENERATION_KEYS) {
          const value = own(options, key);
          if (typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value))) params[key] = value;
        }
        if (Object.keys(params).length) span.setAttribute(SC.LLM_INVOCATION_PARAMETERS, JSON.stringify(params));
      }
    });
    const hideInputs = this.config.hideInputs || this.config.hideInputText || this.config.hideInputMessages;
    const hideOutputs = this.config.hideOutputs || this.config.hideOutputText || this.config.hideOutputMessages;
    if (!hideInputs) capture(span, SC.INPUT_VALUE, qa ? { question: args[0], context: args[1] } : args[0]);
    const success = (result: unknown) => {
      if (embedding) {
        safely(() => {
          const dims = Reflect.get(result as object, "dims");
          if (Array.isArray(dims) && dims.length > 0 && dims.length <= 8 && dims.every((v) => Number.isSafeInteger(v) && v >= 0)) {
            span.setAttribute("transformers.output.shape", dims);
            span.setAttribute("transformers.embedding.dimension", dims[dims.length - 1]);
            span.setAttribute("transformers.output.batch_size", dims[0]);
          }
        });
      // Pipeline outputs can echo their inputs (including QA answers and return_full_text).
      } else if (!hideOutputs && !hideInputs) capture(span, SC.OUTPUT_VALUE, result);
      safely(() => span.end());
      return result;
    };
    const failure = (error: unknown): never => {
      safely(() => {
        // Never forward arbitrary thrown objects, provider bodies or unbounded stacks.
        const message = this.config.hideErrorDetails ? undefined : safely(() => typeof error === "string" ? error : (error as Error)?.message);
        const stack = this.config.hideErrorDetails ? undefined : safely(() => (error as Error)?.stack);
        const name = safely(() => (error as Error)?.name);
        span.recordException({
          name: typeof name === "string" && /^(Error|TypeError|RangeError|SyntaxError|ReferenceError|EvalError|URIError|AbortError)$/.test(name) ? name : "Error",
          message: typeof message === "string" ? message.slice(0, 2048) : undefined,
          stack: typeof stack === "string" ? stack.slice(0, 2048) : undefined,
        });
      });
      safely(() => span.setStatus({ code: SpanStatusCode.ERROR }));
      safely(() => span.end());
      throw error;
    };
    let invoked = false;
    try {
      return context.with(trace.setSpan(parent, span), () => {
        invoked = true;
        let result;
        try { result = Reflect.apply(target, thisArg, args); } catch (error) { return failure(error); }
        // Transformers.js returns promises; also preserve synchronous callable behavior.
        if (result instanceof Promise) return result.then(success, failure);
        return success(result);
      });
    } catch (error) {
      // An application error already passed through failure. Never replay the call.
      if (invoked) throw error;
      safely(() => span.end());
      return Reflect.apply(target, thisArg, args);
    }
  }
}
