import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
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
  LLMProvider,
} from "@traceai/fi-semantic-conventions";
import { isTracingSuppressed } from "@opentelemetry/core";
import {
  FITracer,
  safelyJSONStringify,
  TraceConfigOptions,
  generateTraceConfig,
} from "@traceai/fi-core";

// Ownership is per prototype, not per process: separate SDK copies may coexist.
const patchOwners = new WeakMap<object, GoogleGenAIInstrumentation>();
let patchedCount = 0;
export function isPatched() {
  return patchedCount > 0;
}

// Telemetry is best effort. Never log capture failures: they can contain secrets too.
function capture(fn: () => void): void {
  try { fn(); } catch { /* Preserve application behavior. */ }
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
    // InstrumentationBase calls enable() before our traceConfig is assigned.
    // Rebuild even when the globally registered tracer has not changed, so the
    // first manually or automatically instrumented call honors privacy options.
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig });
  }

  // Lazy fields also work when InstrumentationBase invokes enable during super().
  private manualModules?: Set<Record<string, unknown>>;
  private ownedModules?: Map<Record<string, unknown>, Map<string, unknown>>;

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
    this.manualModules?.forEach(module => this.patch(module));
  }

  public override disable(): void {
    super.disable();
    // The base class only knows about auto-loaded modules (and can already be disabled).
    for (const module of this.ownedModules?.keys() ?? []) this.unpatch(module);
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      "@google/generative-ai", [">=0.1.0"], this.patch.bind(this), this.unpatch.bind(this),
    );
  }

  /** Manually instrument the legacy SDK, including when automatic loading is disabled. */
  manuallyInstrument(module: Record<string, unknown>) {
    (this.manualModules ??= new Set()).add(module);
    this.patch(module);
  }

  private patch(module: Record<string, unknown>) {
    const prototype = (module?.GenerativeModel as { prototype?: Record<string, unknown> } | undefined)?.prototype;
    const methods = ["generateContent", "embedContent", "batchEmbedContents"] as const;
    if (!prototype || !methods.some(method => typeof prototype[method] === "function")) {
      diag.warn(
        "@traceai/google-genai only instruments the legacy @google/generative-ai SDK; " +
        "the supplied module has no supported GenerativeModel methods. " +
        "The modern @google/genai SDK is not supported by this adapter.",
      );
      return module;
    }
    if (patchOwners.has(prototype)) return module;
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
    const instrumentation = this;
    const wrappers = new Map<string, unknown>();
    for (const method of methods) {
      if (typeof prototype[method] !== "function") continue;
      this._wrap(prototype, method, original => {
        if (typeof original !== "function") return original;
        return function (this: { model?: string }, ...args: unknown[]) {
          return instrumentation.traceCall(
            method, this, () => original.apply(this, args),
            () => {
              // Normalize only for capture. The SDK always receives the original arguments.
              const request = args[0];
              const config = generateTraceConfig(instrumentation._traceConfig);
              // Opaque legacy payloads can duplicate media/text in raw/value/tools.
              // Hide the whole input surface before serialization, independently of
              // the installed fi-core masking version. Never modify the SDK request.
              if (config.hideInputs || config.hideInputText || config.hideInputMessages || config.hideInputImages) return {};
              if (method === "generateContent") {
                const normalized = normalizeGeneration(request);
                return {
                  [SemanticConventions.INPUT_VALUE]: safelyJSONStringify(normalized) ?? "",
                  [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
                  ...getLLMInputMessagesAttributes(normalized),
                  ...getGenerationConfigAttributes(normalized),
                  [SemanticConventions.RAW_INPUT]: safelyJSONStringify(normalized) ?? "",
                };
              }
              if (method === "embedContent") {
                const normalized = typeof request === "string" || Array.isArray(request)
                  ? { content: normalizeGeneration(request).contents[0] }
                  : request as EmbedContentRequest;
                const inputText = normalized?.content?.parts?.map(p => p.text ?? "").join("") ?? "";
                return {
                  [SemanticConventions.INPUT_VALUE]: inputText,
                  [SemanticConventions.INPUT_MIME_TYPE]: MimeType.TEXT,
                  [`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]: inputText,
                  [SemanticConventions.RAW_INPUT]: safelyJSONStringify(normalized) ?? "",
                };
              }
              return {
                [SemanticConventions.INPUT_VALUE]: safelyJSONStringify(request) ?? "",
                [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
                ...getBatchEmbeddingTextAttributes(request as BatchEmbedContentsRequest),
                [SemanticConventions.RAW_INPUT]: safelyJSONStringify(request) ?? "",
              };
            },
            result => {
              const config = generateTraceConfig(instrumentation._traceConfig);
              if (method === "generateContent") {
                const response = result as GenerateContentResponse;
                const metadata = {
                  [SemanticConventions.GEN_AI_RESPONSE_MODEL]: this.model || "unknown",
                  ...getUsageAttributes(response),
                };
                if (config.hideOutputs || config.hideOutputText || config.hideOutputMessages) return metadata;
                // SDK text() throws on SAFETY / blocked responses, although the call resolved.
                const text = response.response?.candidates?.[0]?.content?.parts
                  ?.map(part => part.text ?? "").join("") ?? "";
                return {
                  [SemanticConventions.OUTPUT_VALUE]: text,
                  [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
                  ...metadata,
                  ...getLLMOutputMessagesAttributes(response),
                  [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(response) ?? "",
                };
              }
              // Vectors also occur inside raw.output, not only embedding.vector.
              if (config.hideOutputs || config.hideEmbeddingVectors) return {};
              return {
                ...(method === "embedContent"
                  ? { [`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_VECTOR}`]:
                    (result as EmbedContentResponse).embedding.values }
                  : getBatchEmbeddingVectorAttributes(result as BatchEmbedContentsResponse)),
                [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
              };
            },
          );
        };
      });
      wrappers.set(method, prototype[method]);
    }
    (this.ownedModules ??= new Map()).set(module, wrappers);
    patchOwners.set(prototype, this);
    patchedCount++;
    capture(() => { module.fiPatched = true; });
    return module;
  }

  private traceCall<T>(
    method: string,
    model: { model?: string },
    invoke: () => Promise<T>,
    input: () => Attributes,
    output: (result: T) => Attributes,
  ): Promise<T> {
    if (isTracingSuppressed(context.active())) return invoke();
    let span: Span;
    try {
      const generation = method === "generateContent";
      span = this.fiTracer.startSpan(`Google GenAI ${method}`, {
        kind: SpanKind.INTERNAL,
        attributes: {
          [SemanticConventions.FI_SPAN_KIND]: generation ? FISpanKind.LLM : FISpanKind.EMBEDDING,
          [generation ? SemanticConventions.LLM_MODEL_NAME : SemanticConventions.EMBEDDING_MODEL_NAME]: model.model || "unknown",
          [SemanticConventions.LLM_PROVIDER]: LLMProvider.GOOGLE_GENERATIVE_AI,
          [SemanticConventions.GEN_AI_OPERATION_NAME]: generation ? "chat" : "embeddings",
        },
      });
    } catch { return invoke(); }
    capture(() => span.setAttributes(input()));
    const finish = (failed: boolean) => {
      // Provider errors may echo credentials (including malformed header values).
      // Never export their message, type or stack, even when content capture is enabled.
      capture(() => {
        if (failed) span.setAttribute("error.type", "provider_error");
        span.setStatus({ code: failed ? SpanStatusCode.ERROR : SpanStatusCode.OK });
      });
      capture(() => span.end());
    };
    let promise: Promise<T>;
    try {
      promise = context.with(trace.setSpan(context.active(), span), invoke);
    } catch (error) {
      finish(true);
      throw error;
    }
    return Promise.resolve(promise).then(result => {
      capture(() => span.setAttributes(output(result)));
      finish(false);
      return result;
    }, error => {
      finish(true);
      throw error;
    });
  }

  private unpatch(module: Record<string, unknown>) {
    const wrappers = this.ownedModules?.get(module);
    if (!wrappers) return; // Never remove another instrumentation instance's wrappers.
    const prototype = (module.GenerativeModel as { prototype: Record<string, unknown> }).prototype;
    for (const [method, wrapper] of wrappers) {
      if (prototype[method] === wrapper) this._unwrap(prototype, method);
    }
    this.ownedModules!.delete(module);
    patchOwners.delete(prototype);
    patchedCount--;
    capture(() => { module.fiPatched = false; });
  }

}

/**
 * Converts the request to LLM input messages (JSON blob)
 */
function normalizeGeneration(request: unknown): GenerateContentRequest {
  if (typeof request === "string" || Array.isArray(request)) {
    const parts = (Array.isArray(request) ? request : [request])
      .map(part => typeof part === "string" ? { text: part } : part);
    return { contents: [{ role: "user", parts }] };
  }
  return request as GenerateContentRequest;
}

function getLLMInputMessagesAttributes(request: GenerateContentRequest): Attributes {
  const messages: Array<Record<string, unknown>> = [];

  // Add system instruction if present
  if (request.systemInstruction?.parts) {
    const systemText = request.systemInstruction.parts.map(p => p.text).join('');
    messages.push({ role: 'system', content: systemText });
  }

  // Add content messages
  request.contents.forEach((content) => {
    const textParts = content.parts.filter(p => p.text).map(p => p.text).join('');
    const msg: Record<string, unknown> = { role: content.role || 'user' };
    if (textParts) {
      msg.content = textParts;
    }
    messages.push(msg);
  });

  const attributes: Attributes = {
    [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(messages) ?? "[]",
  };

  // Add tools if present
  if (request.tools) {
    const allFunctions = request.tools.flatMap(tool => tool.functionDeclarations || []);
    if (allFunctions.length > 0) {
      attributes[SemanticConventions.LLM_TOOLS] = safelyJSONStringify(allFunctions) ?? "[]";
    }
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
 * Converts the response to LLM output messages (JSON blob)
 */
function getLLMOutputMessagesAttributes(result: GenerateContentResponse): Attributes {
  const candidate = result.response.candidates?.[0];
  if (!candidate?.content) {
    return {};
  }

  const msg: Record<string, unknown> = { role: candidate.content.role || 'model' };

  const textParts = candidate.content.parts.filter(p => p.text).map(p => p.text).join('');
  if (textParts) {
    msg.content = textParts;
  }

  // Handle function calls
  const toolCalls = candidate.content.parts
    .filter(part => part.functionCall)
    .map(part => ({
      function: { name: part.functionCall!.name, arguments: JSON.stringify(part.functionCall!.args) },
    }));
  if (toolCalls.length > 0) {
    msg.tool_calls = toolCalls;
  }

  return {
    [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([msg]) ?? "[]",
    [SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS]: safelyJSONStringify(
      result.response.candidates?.map(c => c.finishReason).filter(Boolean)
    ) ?? "[]",
  };
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
