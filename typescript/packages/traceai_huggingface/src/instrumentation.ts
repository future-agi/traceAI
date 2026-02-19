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
  Span,
} from "@opentelemetry/api";
import { VERSION } from "./version";
import {
  SemanticConventions,
  FISpanKind,
  MimeType,
  LLMSystem,
  LLMProvider,
} from "@traceai/fi-semantic-conventions";
import { isTracingSuppressed } from "@opentelemetry/core";
import {
  FITracer,
  safelyJSONStringify,
  TraceConfigOptions,
} from "@traceai/fi-core";

const MODULE_NAME = "@huggingface/inference";

/**
 * Flag to check if the huggingface module has been patched
 */
let _isFIPatched = false;

/**
 * Function to check if instrumentation is enabled / disabled
 */
export function isPatched() {
  return _isFIPatched;
}

/**
 * Resolves the execution context for the current span
 */
function getExecContext(span: Span) {
  const activeContext = context.active();
  const suppressTracing = isTracingSuppressed(activeContext);
  const execContext = suppressTracing
    ? trace.setSpan(context.active(), span)
    : activeContext;
  if (suppressTracing) {
    trace.deleteSpan(activeContext);
  }
  return execContext;
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

interface HFStream<T> {
  [Symbol.asyncIterator](): AsyncIterator<T>;
}

interface HFChatCompletionStreamOutput {
  choices: Array<{
    delta: {
      content?: string;
      role?: string;
    };
    index: number;
    finish_reason?: string;
  }>;
}

/**
 * @param instrumentationConfig The config for the instrumentation
 * @param traceConfig The FI trace configuration for masking sensitive information
 */
export class HuggingFaceInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;

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
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
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
   * Manually instruments the HuggingFace module
   */
  manuallyInstrument(module: Record<string, unknown>) {
    this.patch(module);
  }

  /**
   * Patches the HuggingFace module
   */
  private patch(
    module: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    if (module?.fiPatched || _isFIPatched) {
      return module;
    }

    const instrumentation: HuggingFaceInstrumentation = this;

    // Ensure fiTracer uses the most up-to-date tracer
    if (!(instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer ||
        (instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer !== instrumentation.tracer) {
      instrumentation.fiTracer = new FITracer({
        tracer: instrumentation.tracer,
        traceConfig: instrumentation._traceConfig
      });
    }

    // Patch the HfInference class
    const HfInference = module.HfInference as {
      prototype: Record<string, unknown>;
    } | undefined;

    if (HfInference?.prototype) {
      // Patch textGeneration
      if (typeof HfInference.prototype.textGeneration === "function") {
        this._wrap(
          HfInference.prototype,
          "textGeneration",
          this.createTextGenerationWrapper(instrumentation),
        );
      }

      // Patch chatCompletion
      if (typeof HfInference.prototype.chatCompletion === "function") {
        this._wrap(
          HfInference.prototype,
          "chatCompletion",
          this.createChatCompletionWrapper(instrumentation),
        );
      }

      // Patch chatCompletionStream
      if (typeof HfInference.prototype.chatCompletionStream === "function") {
        this._wrap(
          HfInference.prototype,
          "chatCompletionStream",
          this.createChatCompletionStreamWrapper(instrumentation),
        );
      }

      // Patch featureExtraction (embeddings)
      if (typeof HfInference.prototype.featureExtraction === "function") {
        this._wrap(
          HfInference.prototype,
          "featureExtraction",
          this.createFeatureExtractionWrapper(instrumentation),
        );
      }

      // Patch summarization
      if (typeof HfInference.prototype.summarization === "function") {
        this._wrap(
          HfInference.prototype,
          "summarization",
          this.createSummarizationWrapper(instrumentation),
        );
      }

      // Patch translation
      if (typeof HfInference.prototype.translation === "function") {
        this._wrap(
          HfInference.prototype,
          "translation",
          this.createTranslationWrapper(instrumentation),
        );
      }

      // Patch questionAnswering
      if (typeof HfInference.prototype.questionAnswering === "function") {
        this._wrap(
          HfInference.prototype,
          "questionAnswering",
          this.createQuestionAnsweringWrapper(instrumentation),
        );
      }
    }

    // Also patch InferenceClient if it exists
    const InferenceClient = module.InferenceClient as {
      prototype: Record<string, unknown>;
    } | undefined;

    if (InferenceClient?.prototype) {
      const methodsToPatch = [
        "textGeneration",
        "chatCompletion",
        "chatCompletionStream",
        "featureExtraction",
        "summarization",
        "translation",
        "questionAnswering",
      ];

      methodsToPatch.forEach((method) => {
        if (typeof InferenceClient.prototype[method] === "function") {
          const wrapper = this.getWrapperForMethod(method, instrumentation);
          if (wrapper) {
            this._wrap(InferenceClient.prototype, method, wrapper);
          }
        }
      });
    }

    _isFIPatched = true;
    try {
      module.fiPatched = true;
    } catch (e) {
      diag.warn(`Failed to set fiPatched flag on module '${MODULE_NAME}'. Error: ${e}`);
    }

    return module;
  }

  private getWrapperForMethod(method: string, instrumentation: HuggingFaceInstrumentation) {
    switch (method) {
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
      ) {
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
              [`${SemanticConventions.LLM_PROMPTS}.0`]: args.inputs,
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(args) ?? "",
            },
          },
        );

        const execContext = getExecContext(span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [args]);
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
      ) {
        const span = instrumentation.fiTracer.startSpan(
          `HuggingFace Chat Completion`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: args.model ?? "unknown",
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(args),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.HUGGINGFACE,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
              ...getChatInputMessagesAttributes(args),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(args) ?? "",
            },
          },
        );

        const execContext = getExecContext(span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [args]);
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

  /**
   * Creates a wrapper for chatCompletionStream
   */
  private createChatCompletionStreamWrapper(instrumentation: HuggingFaceInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedChatCompletionStream(
        this: unknown,
        args: HFChatCompletionInput,
      ) {
        const span = instrumentation.fiTracer.startSpan(
          `HuggingFace Chat Completion Stream`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: args.model ?? "unknown",
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(args),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.HUGGINGFACE,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
              ...getChatInputMessagesAttributes(args),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(args) ?? "",
            },
          },
        );

        const execContext = getExecContext(span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [args]);
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

        const wrappedPromise = (execPromise as Promise<HFStream<HFChatCompletionStreamOutput>>).then((stream) => {
          return wrapChatStream(stream, span);
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
        });

        return context.bind(execContext, wrappedPromise) as Promise<AsyncGenerator<HFChatCompletionStreamOutput>>;
      };
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
      ) {
        const inputTexts = Array.isArray(args.inputs) ? args.inputs : [args.inputs];

        const span = instrumentation.fiTracer.startSpan(
          `HuggingFace Feature Extraction`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.EMBEDDING,
              [SemanticConventions.EMBEDDING_MODEL_NAME]: args.model ?? "unknown",
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(args),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.HUGGINGFACE,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "embeddings",
              ...getEmbeddingInputAttributes(inputTexts),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(args) ?? "",
            },
          },
        );

        const execContext = getExecContext(span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [args]);
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
            ...getEmbeddingOutputAttributes(result, inputTexts.length),
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
      ) {
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
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(args) ?? "",
            },
          },
        );

        const execContext = getExecContext(span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [args]);
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
      ) {
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
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(args) ?? "",
            },
          },
        );

        const execContext = getExecContext(span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [args]);
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
      ) {
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
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(args) ?? "",
            },
          },
        );

        const execContext = getExecContext(span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [args]);
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

  /**
   * Un-patches the HuggingFace module
   */
  private unpatch(
    moduleExports: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    const HfInference = moduleExports.HfInference as {
      prototype: Record<string, unknown>;
    } | undefined;

    const methodsToUnpatch = [
      "textGeneration",
      "chatCompletion",
      "chatCompletionStream",
      "featureExtraction",
      "summarization",
      "translation",
      "questionAnswering",
    ];

    if (HfInference?.prototype) {
      methodsToUnpatch.forEach((method) => {
        if (typeof HfInference.prototype[method] === "function") {
          this._unwrap(HfInference.prototype, method);
        }
      });
    }

    const InferenceClient = moduleExports.InferenceClient as {
      prototype: Record<string, unknown>;
    } | undefined;

    if (InferenceClient?.prototype) {
      methodsToUnpatch.forEach((method) => {
        if (typeof InferenceClient.prototype[method] === "function") {
          this._unwrap(InferenceClient.prototype, method);
        }
      });
    }

    _isFIPatched = false;
    try {
      moduleExports.fiPatched = false;
    } catch (e) {
      diag.warn(`Failed to unset fiPatched flag on module '${MODULE_NAME}'. Error: ${e}`);
    }
  }
}

/**
 * Wraps a chat stream to capture the full response
 */
async function* wrapChatStream(
  stream: HFStream<HFChatCompletionStreamOutput>,
  span: Span,
): AsyncGenerator<HFChatCompletionStreamOutput, void, unknown> {
  let fullContent = "";
  const allChunks: HFChatCompletionStreamOutput[] = [];

  try {
    for await (const chunk of stream) {
      allChunks.push(chunk);
      const choice = chunk.choices?.[0];
      if (choice?.delta?.content) {
        fullContent += choice.delta.content;
      }
      yield chunk;
    }
  } catch (error) {
    span.recordException(error as Error);
    span.setStatus({ code: SpanStatusCode.ERROR, message: (error as Error).message });
    span.end();
    throw error;
  }

  const attributes: Attributes = {
    [SemanticConventions.OUTPUT_VALUE]: fullContent,
    [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
    [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([{ role: "assistant", content: fullContent }]) ?? "[]",
    [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(allChunks) ?? "",
  };

  span.setAttributes(attributes);
  span.setStatus({ code: SpanStatusCode.OK });
  span.end();
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
