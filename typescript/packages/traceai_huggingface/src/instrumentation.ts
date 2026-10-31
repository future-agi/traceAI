import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
  safeExecuteInTheMiddle,
} from "@opentelemetry/instrumentation";
import {
  diag,
  context,
  trace,
  SpanKind,
  Attributes,
  SpanStatusCode,
  TracerProvider,
} from "@opentelemetry/api";
import { VERSION } from "./version";
import {
  SemanticConventions,
  FISpanKind,
  MimeType,
  LLMProvider,
} from "@traceai/fi-semantic-conventions";
import { isTracingSuppressed } from "@opentelemetry/core";
import {
  FITracer,
  safelyJSONStringify,
  TraceConfigOptions,
  TraceConfig,
  generateTraceConfig,
} from "@traceai/fi-core";

const MODULE_NAME = "@huggingface/inference";

const patchedInstrumentations = new Set<HuggingFaceInstrumentation>();
const CLIENT_CONSTRUCTORS = new Set([
  "InferenceClient", "HfInference", "InferenceClientEndpoint", "HfInferenceEndpoint",
]);

/** Whether an enabled instrumentor has wrapped a module or client. */
export function isPatched() {
  return patchedInstrumentations.size > 0;
}

// Type definitions for HuggingFace Inference SDK
interface HFTextGenerationInput {
  model?: string;
  inputs: string;
  parameters?: {
    max_new_tokens?: number;
    temperature?: number;
    top_p?: number;
    top_k?: number;
    repetition_penalty?: number;
    stop_sequences?: string[];
    return_full_text?: boolean;
    [key: string]: unknown;
  };
}

interface HFTextGenerationOutput {
  generated_text: string;
}

interface HFChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

interface HFChatCompletionInput {
  model?: string;
  messages: HFChatMessage[];
  max_tokens?: number;
  temperature?: number;
  top_p?: number;
  stop?: string[];
  stream?: boolean;
  [key: string]: unknown;
}

interface HFChatCompletionOutput {
  choices: Array<{
    index: number;
    message: HFChatMessage;
    finish_reason: string;
  }>;
  created: number;
  id: string;
  model: string;
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

interface HFFeatureExtractionInput {
  model?: string;
  inputs: string | string[];
}

interface HFSummarizationInput {
  model?: string;
  inputs: string;
  parameters?: {
    max_length?: number;
    min_length?: number;
    [key: string]: unknown;
  };
}

interface HFSummarizationOutput {
  summary_text: string;
}

interface HFTranslationInput {
  model?: string;
  inputs: string;
}

interface HFTranslationOutput {
  translation_text: string;
}

interface HFQuestionAnsweringInput {
  model?: string;
  inputs: {
    question: string;
    context: string;
  };
}

interface HFQuestionAnsweringOutput {
  answer: string;
  score: number;
  start: number;
  end: number;
}

/**
 * @param instrumentationConfig The config for the instrumentation
 * @param traceConfig The FI trace configuration for masking sensitive information
 */
export class HuggingFaceInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private resolvedTraceConfig!: TraceConfig;
  private readonly clients = new WeakMap<object, object>();
  private readonly constructors = new WeakMap<object, object>();
  private readonly moduleSources = new WeakMap<Record<string, unknown>, Record<string, unknown>>();
  private readonly modules = new Map<Record<string, unknown>, {
    source: Record<string, unknown>;
    wrapped: Record<string, unknown>;
    originals: Map<string, PropertyDescriptor>;
    replacements: Map<string, unknown>;
  }>();

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super(
      "@traceai/huggingface",
      VERSION,
      Object.assign({}, instrumentationConfig),
    );
    this._traceConfig = traceConfig;
    this.refreshTracer();
  }

  public override enable(): void {
    super.enable();
    this.refreshTracer();
    if (this.modules?.size) patchedInstrumentations.add(this);
  }

  public override setTracerProvider(provider: TracerProvider): void {
    super.setTracerProvider(provider);
    this.refreshTracer();
  }

  private refreshTracer(): void {
    this.resolvedTraceConfig = generateTraceConfig(this._traceConfig);
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this.resolvedTraceConfig });
  }

  public override disable(): void {
    super.disable();
    // OTel does not know about modules patched through manuallyInstrument().
    for (const module of this.modules?.keys() ?? []) this.unpatch(module);
    patchedInstrumentations.delete(this);
  }

  public readonly instrumentationName = "@traceai/huggingface";

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "@huggingface/inference",
      [">=2.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  /**
   * Use the returned module, especially with ESM/read-only exports. Mutable
   * CommonJS exports are also patched in place for existing callers.
   */
  manuallyInstrument<T extends Record<string, unknown>>(module: T): T {
    return this.patch(module) as T;
  }

  /** Wrap an already-created client without changing its immutable task methods. */
  instrumentClient<T extends object>(client: T): T {
    const existing = this.clients.get(client);
    if (existing) return existing as T;
    const instrumentation = this;
    const methods = new Map<PropertyKey, { original: unknown; wrapped: unknown }>();
    // A proxy of client itself cannot replace a nonconfigurable, nonwritable
    // property. Inherit from it instead; instanceof and the SDK receiver survive.
    const wrapped = new Proxy(Object.create(client), {
      get(_target, key) {
        const original = Reflect.get(client, key, client);
        if (typeof original !== "function" || key === "constructor") return original;
        const cached = methods.get(key);
        if (cached?.original === original) return cached.wrapped;
        const bound = original.bind(client);
        const factory = typeof key === "string"
          ? instrumentation.getWrapperForMethod(key, instrumentation) : null;
        const method = key === "endpoint"
          ? (...args: unknown[]) => instrumentation.instrumentClient(bound(...args))
          : factory ? factory(bound) : bound;
        methods.set(key, { original, wrapped: method });
        return method;
      },
      set(_target, key, value) {
        return Reflect.set(client, key, value, client);
      },
    }) as T;
    this.clients.set(client, wrapped);
    this.clients.set(wrapped, wrapped);
    if (this.isEnabled()) patchedInstrumentations.add(this);
    return wrapped;
  }

  private wrapConstructor(original: Function): Function {
    const existing = this.constructors.get(original);
    if (existing) return existing as Function;
    const instrumentation = this;
    const wrapped = new Proxy(original, {
      construct(target, args, newTarget) {
        return instrumentation.instrumentClient(Reflect.construct(target, args, newTarget));
      },
    });
    this.constructors.set(original, wrapped);
    this.constructors.set(wrapped, wrapped);
    return wrapped;
  }

  private patch(module: Record<string, unknown>) {
    module = this.moduleSources.get(module) ?? module;
    const existing = this.modules.get(module);
    if (existing) return existing.wrapped;
    const wrapped: Record<string, unknown> = Object.create(Object.getPrototypeOf(module));
    const originals = new Map<string, PropertyDescriptor>();
    const replacements = new Map<string, unknown>();
    for (const key of Object.getOwnPropertyNames(module)) {
      const original = module[key];
      const factory = this.getWrapperForMethod(key, this);
      const value = typeof original !== "function" ? original
        : CLIENT_CONSTRUCTORS.has(key) ? this.wrapConstructor(original)
        : factory ? factory(original.bind(module)) : original;
      Object.defineProperty(wrapped, key, { value, enumerable: true, configurable: true, writable: true });
      if (value === original) continue;
      replacements.set(key, value);
      const descriptor = Object.getOwnPropertyDescriptor(module, key)!;
      // CJS 4.x exports are configurable. ESM namespaces and older SDK exports
      // are immutable, so their caller must use the returned facade.
      try {
        if (descriptor.configurable) {
          Object.defineProperty(module, key, { value, enumerable: descriptor.enumerable, configurable: true, writable: true });
          originals.set(key, descriptor);
        } else if (descriptor.writable && Reflect.set(module, key, value)) {
          originals.set(key, descriptor);
        }
      } catch (error) {
        // Export mutation is best effort; the returned wrapper still works.
        diag.debug(`${MODULE_NAME}: could not replace export ${key}; using the returned wrapper`, error);
      }
    }
    if (replacements.size === 0) return module;
    Object.defineProperty(wrapped, "fiPatched", { get: () => this.isEnabled(), configurable: true });
    const record = { source: module, wrapped, originals, replacements };
    this.modules.set(module, record);
    this.modules.set(wrapped, record);
    this.moduleSources.set(wrapped, module);
    if (this.isEnabled()) patchedInstrumentations.add(this);
    if (originals.size < replacements.size) {
      diag.debug(`${MODULE_NAME}: use the module returned by manuallyInstrument() for read-only exports`);
    }
    return wrapped;
  }

  private getWrapperForMethod(method: string, instrumentation: HuggingFaceInstrumentation) {
    switch (method) {
      case "textGenerationStream":
        return this.createStreamingWrapper(instrumentation, false);
      case "textGeneration":
        return this.createTextGenerationWrapper(instrumentation);
      case "chatCompletion":
        return this.createChatCompletionWrapper(instrumentation);
      case "chatCompletionStream":
        return this.createChatCompletionStreamWrapper(instrumentation);
      case "featureExtraction":
        return this.createFeatureExtractionWrapper(instrumentation);
      case "summarization":
        return this.createSummarizationWrapper(instrumentation);
      case "translation":
        return this.createTranslationWrapper(instrumentation);
      case "questionAnswering":
        return this.createQuestionAnsweringWrapper(instrumentation);
      default:
        return null;
    }
  }

  /**
   * Creates a wrapper for textGeneration
   */
  private createTextGenerationWrapper(instrumentation: HuggingFaceInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedTextGeneration(
        this: unknown,
        args: HFTextGenerationInput,
        ...options: unknown[]
      ) {
        if (!instrumentation.isEnabled() || isTracingSuppressed(context.active())) {
          return original.apply(this, [args, ...options]);
        }
        const span = instrumentation.fiTracer.startSpan(
          `HuggingFace Text Generation`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: args.model ?? "unknown",
              [SemanticConventions.INPUT_VALUE]: args.inputs,
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.TEXT,
              [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                JSON.stringify(args.parameters ?? {}),
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.HUGGINGFACE,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "text_completion",
              ...(!instrumentation.resolvedTraceConfig.hideInputs ? { [`${SemanticConventions.LLM_PROMPTS}.0`]: args.inputs } : {}),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(safeInput(args)) ?? "",
            },
          },
        );

        const execContext = context.active();
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [args, ...options]);
            });
          },
          (error) => {
            if (error) {
              span.recordException(error);
              span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
              span.end();
            }
          },
        );

        const wrappedPromise = (execPromise as Promise<HFTextGenerationOutput>).then((result) => {
          span.setAttributes({
            [SemanticConventions.OUTPUT_VALUE]: result.generated_text,
            [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
            [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
          });
          span.setStatus({ code: SpanStatusCode.OK });
          span.end();
          return result;
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
        });

        return context.bind(execContext, wrappedPromise) as Promise<HFTextGenerationOutput>;
      };
    };
  }

  /**
   * Creates a wrapper for chatCompletion
   */
  private createChatCompletionWrapper(instrumentation: HuggingFaceInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedChatCompletion(
        this: unknown,
        args: HFChatCompletionInput,
        ...options: unknown[]
      ) {
        if (!instrumentation.isEnabled() || isTracingSuppressed(context.active())) {
          return original.apply(this, [args, ...options]);
        }
        const span = instrumentation.fiTracer.startSpan(
          `HuggingFace Chat Completion`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: args.model ?? "unknown",
              [SemanticConventions.INPUT_VALUE]: safelyJSONStringify(safeInput(args)) ?? "",
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.HUGGINGFACE,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
              ...getChatInputMessagesAttributes(args),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(safeInput(args)) ?? "",
            },
          },
        );

        const execContext = context.active();
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [args, ...options]);
            });
          },
          (error) => {
            if (error) {
              span.recordException(error);
              span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
              span.end();
            }
          },
        );

        const wrappedPromise = (execPromise as Promise<HFChatCompletionOutput>).then((result) => {
          span.setAttributes({
            [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
            [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
            [SemanticConventions.LLM_MODEL_NAME]: result.model,
            [SemanticConventions.GEN_AI_RESPONSE_MODEL]: result.model,
            [SemanticConventions.GEN_AI_RESPONSE_ID]: result.id,
            [SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS]: safelyJSONStringify(result.choices.map(c => c.finish_reason)) ?? "[]",
            ...getChatOutputMessagesAttributes(result),
            ...getUsageAttributes(result),
            [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
          });
          span.setStatus({ code: SpanStatusCode.OK });
          span.end();
          return result;
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
        });

        return context.bind(execContext, wrappedPromise) as Promise<HFChatCompletionOutput>;
      };
    };
  }

  private createChatCompletionStreamWrapper(instrumentation: HuggingFaceInstrumentation) {
    return this.createStreamingWrapper(instrumentation, true);
  }

  private createStreamingWrapper(instrumentation: HuggingFaceInstrumentation, chat: boolean) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any) => function (this: unknown, ...args: any[]) {
      const receiver = this;
      const parent = context.active();
      // Keep the SDK's synchronous AsyncIterable API, including for-await without
      // an await. Start lazily so an unconsumed iterator does not leak a span.
      return (async function* () {
        if (!instrumentation.isEnabled() || isTracingSuppressed(parent)) {
          yield* await original.apply(receiver, args);
          return;
        }
        const input = args[0];
        const span = instrumentation.fiTracer.startSpan(
          chat ? "HuggingFace Chat Completion Stream" : "HuggingFace Text Generation Stream",
          { kind: SpanKind.INTERNAL, attributes: {
            [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
            [SemanticConventions.LLM_MODEL_NAME]: input.model ?? "unknown",
            [SemanticConventions.LLM_PROVIDER]: LLMProvider.HUGGINGFACE,
            [SemanticConventions.GEN_AI_OPERATION_NAME]: chat ? "chat" : "text_completion",
            [SemanticConventions.INPUT_VALUE]: chat ? safelyJSONStringify(safeInput(input)) ?? "" : input.inputs,
            [SemanticConventions.INPUT_MIME_TYPE]: chat ? MimeType.JSON : MimeType.TEXT,
            [SemanticConventions.RAW_INPUT]: safelyJSONStringify(safeInput(input)) ?? "",
            ...(chat ? getChatInputMessagesAttributes(input) : {}),
          } }, parent,
        );
        const active = trace.setSpan(parent, span);
        let iterator: AsyncIterator<any> | undefined;
        let complete = false;
        let content = "";
        try {
          const stream = await context.with(active, () => original.apply(receiver, args));
          iterator = stream[Symbol.asyncIterator]();
          while (true) {
            const item = await context.with(active, () => iterator!.next());
            if (item.done) { complete = true; break; }
            const chunk = item.value;
            if (chat) {
              content += chunk.choices?.[0]?.delta?.content ?? "";
              span.setAttributes(getUsageAttributes(chunk));
              if (chunk.model) span.setAttribute(SemanticConventions.GEN_AI_RESPONSE_MODEL, chunk.model);
              if (chunk.id) span.setAttribute(SemanticConventions.GEN_AI_RESPONSE_ID, chunk.id);
            } else {
              content = chunk.generated_text ?? (content + (chunk.token?.text ?? chunk.choices?.[0]?.text ?? ""));
            }
            yield chunk;
          }
          span.setStatus({ code: SpanStatusCode.OK });
        } catch (error) {
          span.recordException(error as Error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: (error as Error).message });
          throw error;
        } finally {
          try {
            if (!complete && iterator?.return) {
              await context.with(active, () => iterator!.return!());
            }
          } finally {
            span.setAttributes({
              [SemanticConventions.OUTPUT_VALUE]: content,
              [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
              ...(chat ? { [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([{ role: "assistant", content }]) ?? "[]" } : {}),
            });
            span.end();
          }
        }
      })();
    };
  }

  /**
   * Creates a wrapper for featureExtraction (embeddings)
   */
  private createFeatureExtractionWrapper(instrumentation: HuggingFaceInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedFeatureExtraction(
        this: unknown,
        args: HFFeatureExtractionInput,
        ...options: unknown[]
      ) {
        if (!instrumentation.isEnabled() || isTracingSuppressed(context.active())) {
          return original.apply(this, [args, ...options]);
        }
        const inputTexts = Array.isArray(args.inputs) ? args.inputs : [args.inputs];

        const span = instrumentation.fiTracer.startSpan(
          `HuggingFace Feature Extraction`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.EMBEDDING,
              [SemanticConventions.EMBEDDING_MODEL_NAME]: args.model ?? "unknown",
              [SemanticConventions.INPUT_VALUE]: safelyJSONStringify(safeInput(args)) ?? "",
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.HUGGINGFACE,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "embeddings",
              ...(!instrumentation.resolvedTraceConfig.hideInputs ? getEmbeddingInputAttributes(inputTexts) : {}),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(safeInput(args)) ?? "",
            },
          },
        );

        const execContext = context.active();
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [args, ...options]);
            });
          },
          (error) => {
            if (error) {
              span.recordException(error);
              span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
              span.end();
            }
          },
        );

        const wrappedPromise = (execPromise as Promise<number[] | number[][]>).then((result) => {
          span.setAttributes({
            [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
            [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
            ...(!instrumentation.resolvedTraceConfig.hideOutputs ? getEmbeddingOutputAttributes(result, inputTexts.length) : {}),
            [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
          });
          span.setStatus({ code: SpanStatusCode.OK });
          span.end();
          return result;
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
        });

        return context.bind(execContext, wrappedPromise) as Promise<number[] | number[][]>;
      };
    };
  }

  /**
   * Creates a wrapper for summarization
   */
  private createSummarizationWrapper(instrumentation: HuggingFaceInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedSummarization(
        this: unknown,
        args: HFSummarizationInput,
        ...options: unknown[]
      ) {
        if (!instrumentation.isEnabled() || isTracingSuppressed(context.active())) {
          return original.apply(this, [args, ...options]);
        }
        const span = instrumentation.fiTracer.startSpan(
          `HuggingFace Summarization`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: args.model ?? "unknown",
              [SemanticConventions.INPUT_VALUE]: args.inputs,
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.TEXT,
              [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                JSON.stringify(args.parameters ?? {}),
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.HUGGINGFACE,
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(safeInput(args)) ?? "",
            },
          },
        );

        const execContext = context.active();
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [args, ...options]);
            });
          },
          (error) => {
            if (error) {
              span.recordException(error);
              span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
              span.end();
            }
          },
        );

        const wrappedPromise = (execPromise as Promise<HFSummarizationOutput>).then((result) => {
          span.setAttributes({
            [SemanticConventions.OUTPUT_VALUE]: result.summary_text,
            [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
            [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
          });
          span.setStatus({ code: SpanStatusCode.OK });
          span.end();
          return result;
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
        });

        return context.bind(execContext, wrappedPromise) as Promise<HFSummarizationOutput>;
      };
    };
  }

  /**
   * Creates a wrapper for translation
   */
  private createTranslationWrapper(instrumentation: HuggingFaceInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedTranslation(
        this: unknown,
        args: HFTranslationInput,
        ...options: unknown[]
      ) {
        if (!instrumentation.isEnabled() || isTracingSuppressed(context.active())) {
          return original.apply(this, [args, ...options]);
        }
        const span = instrumentation.fiTracer.startSpan(
          `HuggingFace Translation`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: args.model ?? "unknown",
              [SemanticConventions.INPUT_VALUE]: args.inputs,
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.TEXT,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.HUGGINGFACE,
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(safeInput(args)) ?? "",
            },
          },
        );

        const execContext = context.active();
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [args, ...options]);
            });
          },
          (error) => {
            if (error) {
              span.recordException(error);
              span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
              span.end();
            }
          },
        );

        const wrappedPromise = (execPromise as Promise<HFTranslationOutput>).then((result) => {
          span.setAttributes({
            [SemanticConventions.OUTPUT_VALUE]: result.translation_text,
            [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
            [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
          });
          span.setStatus({ code: SpanStatusCode.OK });
          span.end();
          return result;
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
        });

        return context.bind(execContext, wrappedPromise) as Promise<HFTranslationOutput>;
      };
    };
  }

  /**
   * Creates a wrapper for questionAnswering
   */
  private createQuestionAnsweringWrapper(instrumentation: HuggingFaceInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedQuestionAnswering(
        this: unknown,
        args: HFQuestionAnsweringInput,
        ...options: unknown[]
      ) {
        if (!instrumentation.isEnabled() || isTracingSuppressed(context.active())) {
          return original.apply(this, [args, ...options]);
        }
        const span = instrumentation.fiTracer.startSpan(
          `HuggingFace Question Answering`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: args.model ?? "unknown",
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(args.inputs),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.HUGGINGFACE,
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(safeInput(args)) ?? "",
            },
          },
        );

        const execContext = context.active();
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [args, ...options]);
            });
          },
          (error) => {
            if (error) {
              span.recordException(error);
              span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
              span.end();
            }
          },
        );

        const wrappedPromise = (execPromise as Promise<HFQuestionAnsweringOutput>).then((result) => {
          span.setAttributes({
            [SemanticConventions.OUTPUT_VALUE]: result.answer,
            [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
            [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
          });
          span.setStatus({ code: SpanStatusCode.OK });
          span.end();
          return result;
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
        });

        return context.bind(execContext, wrappedPromise) as Promise<HFQuestionAnsweringOutput>;
      };
    };
  }

  private unpatch(module: Record<string, unknown>) {
    const record = this.modules.get(module);
    if (!record) return;
    for (const [key, descriptor] of record.originals) {
      if (record.source[key] === record.replacements.get(key)) {
        Object.defineProperty(record.source, key, descriptor);
      }
    }
    this.modules.delete(record.source);
    this.modules.delete(record.wrapped);
    if (!this.modules.size) patchedInstrumentations.delete(this);
  }
}

/** Do not export credentials accepted by the SDK's standalone task functions. */
function safeInput(input: object): object {
  const { accessToken: _token, apiKey: _key, headers: _headers, ...rest } = input as Record<string, unknown>;
  return rest;
}

/**
 * Gets input message attributes for chat requests (JSON blob)
 */
function getChatInputMessagesAttributes(request: HFChatCompletionInput): Attributes {
  const serialized = request.messages.map((message) => {
    const obj: Record<string, unknown> = { role: message.role };
    if (message.content) {
      obj.content = message.content;
    }
    return obj;
  });
  return { [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(serialized) ?? "[]" };
}

/**
 * Gets output message attributes from chat response (JSON blob)
 */
function getChatOutputMessagesAttributes(response: HFChatCompletionOutput): Attributes {
  const choice = response.choices[0];
  if (!choice) {
    return {};
  }

  const msg: Record<string, unknown> = { role: choice.message.role };
  if (choice.message.content) {
    msg.content = choice.message.content;
  }

  return { [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([msg]) ?? "[]" };
}

/**
 * Gets usage attributes from chat response
 */
function getUsageAttributes(response: HFChatCompletionOutput): Attributes {
  if (response.usage) {
    return {
      [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]: response.usage.completion_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_PROMPT]: response.usage.prompt_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_TOTAL]: response.usage.total_tokens,
    };
  }
  return {};
}

/**
 * Gets embedding input attributes
 */
function getEmbeddingInputAttributes(texts: string[]): Attributes {
  return texts.reduce((acc: Attributes, text, index) => {
    acc[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.${index}.${SemanticConventions.EMBEDDING_TEXT}`] = text;
    return acc;
  }, {});
}

/**
 * Gets embedding output attributes
 */
function getEmbeddingOutputAttributes(result: number[] | number[][], inputCount: number): Attributes {
  const attributes: Attributes = {};

  // Handle both single embedding and batch embeddings
  if (inputCount === 1 && !Array.isArray(result[0])) {
    // Single embedding
    attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_VECTOR}`] =
      JSON.stringify(result);
  } else {
    // Batch embeddings
    (result as number[][]).forEach((embedding, index) => {
      attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.${index}.${SemanticConventions.EMBEDDING_VECTOR}`] =
        JSON.stringify(embedding);
    });
  }

  return attributes;
}
