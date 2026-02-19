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
import { isString } from "./typeUtils";

const MODULE_NAME = "@google/generative-ai";

/**
 * Flag to check if the Google GenAI module has been patched
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

// Type definitions for Google GenAI SDK
interface GenerateContentRequest {
  contents: Array<{
    role?: string;
    parts: Array<{
      text?: string;
      inlineData?: {
        mimeType: string;
        data: string;
      };
    }>;
  }>;
  generationConfig?: {
    temperature?: number;
    topP?: number;
    topK?: number;
    maxOutputTokens?: number;
    stopSequences?: string[];
  };
  safetySettings?: Array<{
    category: string;
    threshold: string;
  }>;
  tools?: Array<{
    functionDeclarations?: Array<{
      name: string;
      description?: string;
      parameters?: Record<string, unknown>;
    }>;
  }>;
  systemInstruction?: {
    parts: Array<{ text: string }>;
  };
}

interface GenerateContentResponse {
  response: {
    text: () => string;
    candidates?: Array<{
      content: {
        role: string;
        parts: Array<{
          text?: string;
          functionCall?: {
            name: string;
            args: Record<string, unknown>;
          };
        }>;
      };
      finishReason?: string;
      safetyRatings?: Array<{
        category: string;
        probability: string;
      }>;
    }>;
    usageMetadata?: {
      promptTokenCount: number;
      candidatesTokenCount: number;
      totalTokenCount: number;
    };
  };
}

interface EmbedContentRequest {
  content: {
    parts: Array<{ text: string }>;
  };
  taskType?: string;
  title?: string;
}

interface EmbedContentResponse {
  embedding: {
    values: number[];
  };
}

interface BatchEmbedContentsRequest {
  requests: EmbedContentRequest[];
}

interface BatchEmbedContentsResponse {
  embeddings: Array<{
    values: number[];
  }>;
}

/**
 * @param instrumentationConfig The config for the instrumentation
 * @param traceConfig The FI trace configuration for masking sensitive information
 */
export class GoogleGenAIInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  public readonly instrumentationName = "@traceai/google-genai";

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super(
      "@traceai/google-genai",
      VERSION,
      Object.assign({}, instrumentationConfig),
    );
    this._traceConfig = traceConfig;
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "@google/generative-ai",
      [">=0.1.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  /**
   * Manually instruments the Google GenAI module
   */
  manuallyInstrument(module: Record<string, unknown>) {
    this.patch(module);
  }

  /**
   * Patches the Google GenAI module
   */
  private patch(
    module: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    if (module?.fiPatched || _isFIPatched) {
      return module;
    }

    const instrumentation: GoogleGenAIInstrumentation = this;

    // Ensure fiTracer uses the most up-to-date tracer
    if (!(instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer ||
        (instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer !== instrumentation.tracer) {
      instrumentation.fiTracer = new FITracer({
        tracer: instrumentation.tracer,
        traceConfig: instrumentation._traceConfig
      });
    }

    // Patch GenerativeModel class
    const GenerativeModel = module.GenerativeModel as {
      prototype?: Record<string, unknown>;
    } | undefined;

    if (GenerativeModel?.prototype) {
      // Patch generateContent method
      if (typeof GenerativeModel.prototype.generateContent === 'function') {
        this._wrap(
          GenerativeModel.prototype,
          "generateContent",
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          (original: any): any => {
            return function patchedGenerateContent(
              this: { model?: string },
              ...args: [GenerateContentRequest | string]
            ) {
              const request = typeof args[0] === 'string'
                ? { contents: [{ parts: [{ text: args[0] }] }] }
                : args[0];

              const modelName = this.model || 'unknown';

              const span = instrumentation.fiTracer.startSpan(
                `Google GenAI generateContent`,
                {
                  kind: SpanKind.INTERNAL,
                  attributes: {
                    [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
                    [SemanticConventions.LLM_MODEL_NAME]: modelName,
                    [SemanticConventions.INPUT_VALUE]: JSON.stringify(request),
                    [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
                    [SemanticConventions.LLM_SYSTEM]: LLMSystem.GOOGLE_GENERATIVE_AI,
                    [SemanticConventions.LLM_PROVIDER]: LLMProvider.GOOGLE_GENERATIVE_AI,
                    ...getLLMInputMessagesAttributes(request),
                    ...getGenerationConfigAttributes(request),
                    [SemanticConventions.RAW_INPUT]: safelyJSONStringify(request) ?? "",
                  },
                },
              );

              const execContext = getExecContext(span);
              const execPromise = safeExecuteInTheMiddle(
                () => {
                  return context.with(trace.setSpan(execContext, span), () => {
                    return original.apply(this, args);
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

              const wrappedPromise = (execPromise as Promise<GenerateContentResponse>).then((result) => {
                span.setAttributes({
                  [SemanticConventions.OUTPUT_VALUE]: result.response.text(),
                  [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
                  ...getLLMOutputMessagesAttributes(result),
                  ...getUsageAttributes(result),
                  [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
                });
                span.setStatus({ code: SpanStatusCode.OK });
                span.end();
                return result;
              });

              return context.bind(execContext, wrappedPromise);
            };
          },
        );
      }

      // Patch embedContent method
      if (typeof GenerativeModel.prototype.embedContent === 'function') {
        this._wrap(
          GenerativeModel.prototype,
          "embedContent",
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          (original: any): any => {
            return function patchedEmbedContent(
              this: { model?: string },
              ...args: [EmbedContentRequest | string]
            ) {
              const request = typeof args[0] === 'string'
                ? { content: { parts: [{ text: args[0] }] } }
                : args[0];

              const modelName = this.model || 'unknown';
              const inputText = request.content?.parts?.[0]?.text || '';

              const span = instrumentation.fiTracer.startSpan(`Google GenAI embedContent`, {
                kind: SpanKind.INTERNAL,
                attributes: {
                  [SemanticConventions.FI_SPAN_KIND]: FISpanKind.EMBEDDING,
                  [SemanticConventions.EMBEDDING_MODEL_NAME]: modelName,
                  [SemanticConventions.INPUT_VALUE]: inputText,
                  [SemanticConventions.INPUT_MIME_TYPE]: MimeType.TEXT,
                  [SemanticConventions.LLM_SYSTEM]: LLMSystem.GOOGLE_GENERATIVE_AI,
                  [SemanticConventions.LLM_PROVIDER]: LLMProvider.GOOGLE_GENERATIVE_AI,
                  [`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]: inputText,
                  [SemanticConventions.RAW_INPUT]: safelyJSONStringify(request) ?? "",
                },
              });

              const execContext = getExecContext(span);
              const execPromise = safeExecuteInTheMiddle(
                () => {
                  return context.with(trace.setSpan(execContext, span), () => {
                    return original.apply(this, args);
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

              const wrappedPromise = (execPromise as Promise<EmbedContentResponse>).then((result) => {
                span.setAttributes({
                  [`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_VECTOR}`]: result.embedding.values,
                  [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
                });
                span.setStatus({ code: SpanStatusCode.OK });
                span.end();
                return result;
              });

              return context.bind(execContext, wrappedPromise);
            };
          },
        );
      }

      // Patch batchEmbedContents method
      if (typeof GenerativeModel.prototype.batchEmbedContents === 'function') {
        this._wrap(
          GenerativeModel.prototype,
          "batchEmbedContents",
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          (original: any): any => {
            return function patchedBatchEmbedContents(
              this: { model?: string },
              ...args: [BatchEmbedContentsRequest]
            ) {
              const request = args[0];
              const modelName = this.model || 'unknown';

              const span = instrumentation.fiTracer.startSpan(`Google GenAI batchEmbedContents`, {
                kind: SpanKind.INTERNAL,
                attributes: {
                  [SemanticConventions.FI_SPAN_KIND]: FISpanKind.EMBEDDING,
                  [SemanticConventions.EMBEDDING_MODEL_NAME]: modelName,
                  [SemanticConventions.INPUT_VALUE]: JSON.stringify(request),
                  [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
                  [SemanticConventions.LLM_SYSTEM]: LLMSystem.GOOGLE_GENERATIVE_AI,
                  [SemanticConventions.LLM_PROVIDER]: LLMProvider.GOOGLE_GENERATIVE_AI,
                  ...getBatchEmbeddingTextAttributes(request),
                  [SemanticConventions.RAW_INPUT]: safelyJSONStringify(request) ?? "",
                },
              });

              const execContext = getExecContext(span);
              const execPromise = safeExecuteInTheMiddle(
                () => {
                  return context.with(trace.setSpan(execContext, span), () => {
                    return original.apply(this, args);
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

              const wrappedPromise = (execPromise as Promise<BatchEmbedContentsResponse>).then((result) => {
                span.setAttributes({
                  ...getBatchEmbeddingVectorAttributes(result),
                  [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
                });
                span.setStatus({ code: SpanStatusCode.OK });
                span.end();
                return result;
              });

              return context.bind(execContext, wrappedPromise);
            };
          },
        );
      }
    }

    _isFIPatched = true;
    try {
      module.fiPatched = true;
    } catch (e) {
      diag.warn(`Failed to set fiPatched flag on module '${MODULE_NAME}'. Error: ${e}`);
    }

    return module;
  }

  /**
   * Un-patches the Google GenAI module
   */
  private unpatch(
    moduleExports: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    const GenerativeModel = moduleExports.GenerativeModel as {
      prototype?: Record<string, unknown>;
    } | undefined;

    if (GenerativeModel?.prototype) {
      if (GenerativeModel.prototype.generateContent) {
        this._unwrap(GenerativeModel.prototype, "generateContent");
      }
      if (GenerativeModel.prototype.embedContent) {
        this._unwrap(GenerativeModel.prototype, "embedContent");
      }
      if (GenerativeModel.prototype.batchEmbedContents) {
        this._unwrap(GenerativeModel.prototype, "batchEmbedContents");
      }
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
 * Converts the request to LLM input messages
 */
function getLLMInputMessagesAttributes(request: GenerateContentRequest): Attributes {
  const attributes: Attributes = {};

  // Add system instruction if present
  if (request.systemInstruction?.parts) {
    const systemText = request.systemInstruction.parts.map(p => p.text).join('');
    attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_ROLE}`] = 'system';
    attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_CONTENT}`] = systemText;
  }

  // Add content messages
  const startIndex = request.systemInstruction ? 1 : 0;
  request.contents.forEach((content, idx) => {
    const index = startIndex + idx;
    const indexPrefix = `${SemanticConventions.LLM_INPUT_MESSAGES}.${index}.`;
    attributes[`${indexPrefix}${SemanticConventions.MESSAGE_ROLE}`] = content.role || 'user';

    const textParts = content.parts.filter(p => p.text).map(p => p.text).join('');
    if (textParts) {
      attributes[`${indexPrefix}${SemanticConventions.MESSAGE_CONTENT}`] = textParts;
    }
  });

  // Add tools if present
  if (request.tools) {
    request.tools.forEach((tool, toolIdx) => {
      tool.functionDeclarations?.forEach((func, funcIdx) => {
        const key = `${SemanticConventions.LLM_TOOLS}.${toolIdx * 10 + funcIdx}.${SemanticConventions.TOOL_JSON_SCHEMA}`;
        attributes[key] = safelyJSONStringify(func) ?? '';
      });
    });
  }

  return attributes;
}

/**
 * Gets generation config attributes
 */
function getGenerationConfigAttributes(request: GenerateContentRequest): Attributes {
  if (!request.generationConfig) {
    return {};
  }
  return {
    [SemanticConventions.LLM_INVOCATION_PARAMETERS]: JSON.stringify(request.generationConfig),
  };
}

/**
 * Gets usage attributes from response
 */
function getUsageAttributes(result: GenerateContentResponse): Attributes {
  const usage = result.response.usageMetadata;
  if (!usage) {
    return {};
  }
  return {
    [SemanticConventions.LLM_TOKEN_COUNT_PROMPT]: usage.promptTokenCount,
    [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]: usage.candidatesTokenCount,
    [SemanticConventions.LLM_TOKEN_COUNT_TOTAL]: usage.totalTokenCount,
  };
}

/**
 * Converts the response to LLM output messages
 */
function getLLMOutputMessagesAttributes(result: GenerateContentResponse): Attributes {
  const candidate = result.response.candidates?.[0];
  if (!candidate) {
    return {};
  }

  const indexPrefix = `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.`;
  const attributes: Attributes = {
    [`${indexPrefix}${SemanticConventions.MESSAGE_ROLE}`]: candidate.content.role || 'model',
  };

  const textParts = candidate.content.parts.filter(p => p.text).map(p => p.text).join('');
  if (textParts) {
    attributes[`${indexPrefix}${SemanticConventions.MESSAGE_CONTENT}`] = textParts;
  }

  // Handle function calls
  candidate.content.parts.forEach((part, idx) => {
    if (part.functionCall) {
      const toolCallPrefix = `${indexPrefix}${SemanticConventions.MESSAGE_TOOL_CALLS}.${idx}.`;
      attributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_NAME}`] = part.functionCall.name;
      attributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}`] =
        JSON.stringify(part.functionCall.args);
    }
  });

  return attributes;
}

/**
 * Gets batch embedding text attributes
 */
function getBatchEmbeddingTextAttributes(request: BatchEmbedContentsRequest): Attributes {
  return request.requests.reduce((acc, req, index) => {
    const text = req.content?.parts?.[0]?.text || '';
    acc[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.${index}.${SemanticConventions.EMBEDDING_TEXT}`] = text;
    return acc;
  }, {} as Attributes);
}

/**
 * Gets batch embedding vector attributes
 */
function getBatchEmbeddingVectorAttributes(response: BatchEmbedContentsResponse): Attributes {
  return response.embeddings.reduce((acc, embedding, index) => {
    acc[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.${index}.${SemanticConventions.EMBEDDING_VECTOR}`] =
      embedding.values;
    return acc;
  }, {} as Attributes);
}
