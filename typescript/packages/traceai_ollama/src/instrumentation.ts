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

const MODULE_NAME = "ollama";

/**
 * Flag to check if the ollama module has been patched
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

// Type definitions for Ollama SDK
interface OllamaMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  images?: string[];
  tool_calls?: OllamaToolCall[];
}

interface OllamaToolCall {
  function: {
    name: string;
    arguments: Record<string, unknown>;
  };
}

interface OllamaTool {
  type: "function";
  function: {
    name: string;
    description: string;
    parameters: {
      type: string;
      required?: string[];
      properties: Record<string, unknown>;
    };
  };
}

interface OllamaChatRequest {
  model: string;
  messages: OllamaMessage[];
  format?: string | Record<string, unknown>;
  options?: Record<string, unknown>;
  stream?: boolean;
  keep_alive?: string | number;
  tools?: OllamaTool[];
}

interface OllamaChatResponse {
  model: string;
  created_at: string;
  message: OllamaMessage;
  done: boolean;
  done_reason?: string;
  total_duration?: number;
  load_duration?: number;
  prompt_eval_count?: number;
  prompt_eval_duration?: number;
  eval_count?: number;
  eval_duration?: number;
}

interface OllamaGenerateRequest {
  model: string;
  prompt: string;
  suffix?: string;
  images?: string[];
  format?: string | Record<string, unknown>;
  options?: Record<string, unknown>;
  system?: string;
  template?: string;
  context?: number[];
  stream?: boolean;
  raw?: boolean;
  keep_alive?: string | number;
}

interface OllamaGenerateResponse {
  model: string;
  created_at: string;
  response: string;
  done: boolean;
  done_reason?: string;
  context?: number[];
  total_duration?: number;
  load_duration?: number;
  prompt_eval_count?: number;
  prompt_eval_duration?: number;
  eval_count?: number;
  eval_duration?: number;
}

interface OllamaEmbedRequest {
  model: string;
  input: string | string[];
  truncate?: boolean;
  options?: Record<string, unknown>;
  keep_alive?: string | number;
}

interface OllamaEmbedResponse {
  model: string;
  embeddings: number[][];
  total_duration?: number;
  load_duration?: number;
  prompt_eval_count?: number;
}

// Legacy embeddings API
interface OllamaEmbeddingsRequest {
  model: string;
  prompt: string;
  options?: Record<string, unknown>;
  keep_alive?: string | number;
}

interface OllamaEmbeddingsResponse {
  embedding: number[];
}

interface OllamaAbortableAsyncIterator<T> {
  [Symbol.asyncIterator](): AsyncIterator<T>;
  abort(): void;
}

/**
 * @param instrumentationConfig The config for the instrumentation
 * @param traceConfig The FI trace configuration for masking sensitive information
 */
export class OllamaInstrumentation extends InstrumentationBase {
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
      "@traceai/ollama",
      VERSION,
      Object.assign({}, instrumentationConfig),
    );
    this._traceConfig = traceConfig;
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  public readonly instrumentationName = "@traceai/ollama";

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "ollama",
      [">=0.4.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  /**
   * Manually instruments the Ollama module
   */
  manuallyInstrument(module: Record<string, unknown>) {
    this.patch(module);
  }

  /**
   * Patches the Ollama module
   */
  private patch(
    module: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    if (module?.fiPatched || _isFIPatched) {
      return module;
    }

    const instrumentation: OllamaInstrumentation = this;

    // Ensure fiTracer uses the most up-to-date tracer
    if (!(instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer ||
        (instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer !== instrumentation.tracer) {
      instrumentation.fiTracer = new FITracer({
        tracer: instrumentation.tracer,
        traceConfig: instrumentation._traceConfig
      });
    }

    // Patch the Ollama class
    const Ollama = module.Ollama as {
      prototype: Record<string, unknown>;
    } | undefined;

    if (Ollama?.prototype) {
      // Patch chat method
      if (typeof Ollama.prototype.chat === "function") {
        this._wrap(
          Ollama.prototype,
          "chat",
          this.createChatWrapper(instrumentation),
        );
      }

      // Patch generate method
      if (typeof Ollama.prototype.generate === "function") {
        this._wrap(
          Ollama.prototype,
          "generate",
          this.createGenerateWrapper(instrumentation),
        );
      }

      // Patch embed method (new API)
      if (typeof Ollama.prototype.embed === "function") {
        this._wrap(
          Ollama.prototype,
          "embed",
          this.createEmbedWrapper(instrumentation),
        );
      }

      // Patch embeddings method (legacy API)
      if (typeof Ollama.prototype.embeddings === "function") {
        this._wrap(
          Ollama.prototype,
          "embeddings",
          this.createEmbeddingsWrapper(instrumentation),
        );
      }
    }

    // Also patch the default export if it exists (for direct imports)
    const defaultOllama = module.default as {
      chat?: unknown;
      generate?: unknown;
      embed?: unknown;
      embeddings?: unknown;
    } | undefined;

    if (defaultOllama) {
      if (typeof defaultOllama.chat === "function") {
        this._wrap(
          defaultOllama as Record<string, unknown>,
          "chat",
          this.createChatWrapper(instrumentation),
        );
      }
      if (typeof defaultOllama.generate === "function") {
        this._wrap(
          defaultOllama as Record<string, unknown>,
          "generate",
          this.createGenerateWrapper(instrumentation),
        );
      }
      if (typeof defaultOllama.embed === "function") {
        this._wrap(
          defaultOllama as Record<string, unknown>,
          "embed",
          this.createEmbedWrapper(instrumentation),
        );
      }
      if (typeof defaultOllama.embeddings === "function") {
        this._wrap(
          defaultOllama as Record<string, unknown>,
          "embeddings",
          this.createEmbeddingsWrapper(instrumentation),
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
   * Creates a wrapper for the chat method
   */
  private createChatWrapper(instrumentation: OllamaInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedChat(
        this: unknown,
        request: OllamaChatRequest,
      ) {
        const { messages: _messages, ...invocationParameters } = request;

        const span = instrumentation.fiTracer.startSpan(
          `Ollama Chat`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: request.model,
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(request),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                JSON.stringify(invocationParameters),
              [SemanticConventions.LLM_SYSTEM]: LLMSystem.OLLAMA,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.OLLAMA,
              ...getChatInputMessagesAttributes(request),
              ...getChatToolsJSONSchema(request),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(request) ?? "",
            },
          },
        );

        const execContext = getExecContext(span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [request]);
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

        const wrappedPromise = (execPromise as Promise<OllamaChatResponse | OllamaAbortableAsyncIterator<OllamaChatResponse>>).then((result) => {
          if (isChatResponse(result)) {
            span.setAttributes({
              [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
              [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_MODEL_NAME]: result.model,
              ...getChatOutputMessagesAttributes(result),
              ...getChatUsageAttributes(result),
              [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
            });
            span.setStatus({ code: SpanStatusCode.OK });
            span.end();
          } else if (isAsyncIterator(result)) {
            return wrapChatStream(result, span);
          }
          return result;
        });

        return context.bind(execContext, wrappedPromise) as Promise<OllamaChatResponse | OllamaAbortableAsyncIterator<OllamaChatResponse>>;
      };
    };
  }

  /**
   * Creates a wrapper for the generate method
   */
  private createGenerateWrapper(instrumentation: OllamaInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedGenerate(
        this: unknown,
        request: OllamaGenerateRequest,
      ) {
        const { prompt: _prompt, ...invocationParameters } = request;

        const span = instrumentation.fiTracer.startSpan(
          `Ollama Generate`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: request.model,
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(request),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                JSON.stringify(invocationParameters),
              [SemanticConventions.LLM_SYSTEM]: LLMSystem.OLLAMA,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.OLLAMA,
              ...getGenerateInputAttributes(request),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(request) ?? "",
            },
          },
        );

        const execContext = getExecContext(span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [request]);
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

        const wrappedPromise = (execPromise as Promise<OllamaGenerateResponse | OllamaAbortableAsyncIterator<OllamaGenerateResponse>>).then((result) => {
          if (isGenerateResponse(result)) {
            span.setAttributes({
              [SemanticConventions.OUTPUT_VALUE]: result.response,
              [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
              [SemanticConventions.LLM_MODEL_NAME]: result.model,
              ...getGenerateUsageAttributes(result),
              [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
            });
            span.setStatus({ code: SpanStatusCode.OK });
            span.end();
          } else if (isAsyncIterator(result)) {
            return wrapGenerateStream(result, span);
          }
          return result;
        });

        return context.bind(execContext, wrappedPromise) as Promise<OllamaGenerateResponse | OllamaAbortableAsyncIterator<OllamaGenerateResponse>>;
      };
    };
  }

  /**
   * Creates a wrapper for the embed method (new API)
   */
  private createEmbedWrapper(instrumentation: OllamaInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedEmbed(
        this: unknown,
        request: OllamaEmbedRequest,
      ) {
        const inputTexts = Array.isArray(request.input) ? request.input : [request.input];

        const span = instrumentation.fiTracer.startSpan(
          `Ollama Embed`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.EMBEDDING,
              [SemanticConventions.EMBEDDING_MODEL_NAME]: request.model,
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(request),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_SYSTEM]: LLMSystem.OLLAMA,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.OLLAMA,
              ...getEmbedInputAttributes(inputTexts),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(request) ?? "",
            },
          },
        );

        const execContext = getExecContext(span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [request]);
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

        const wrappedPromise = (execPromise as Promise<OllamaEmbedResponse>).then((result) => {
          span.setAttributes({
            [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
            [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
            [SemanticConventions.EMBEDDING_MODEL_NAME]: result.model,
            ...getEmbedOutputAttributes(result),
            [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
          });
          span.setStatus({ code: SpanStatusCode.OK });
          span.end();
          return result;
        });

        return context.bind(execContext, wrappedPromise) as Promise<OllamaEmbedResponse>;
      };
    };
  }

  /**
   * Creates a wrapper for the embeddings method (legacy API)
   */
  private createEmbeddingsWrapper(instrumentation: OllamaInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedEmbeddings(
        this: unknown,
        request: OllamaEmbeddingsRequest,
      ) {
        const span = instrumentation.fiTracer.startSpan(
          `Ollama Embeddings`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.EMBEDDING,
              [SemanticConventions.EMBEDDING_MODEL_NAME]: request.model,
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(request),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_SYSTEM]: LLMSystem.OLLAMA,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.OLLAMA,
              [`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]: request.prompt,
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(request) ?? "",
            },
          },
        );

        const execContext = getExecContext(span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [request]);
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

        const wrappedPromise = (execPromise as Promise<OllamaEmbeddingsResponse>).then((result) => {
          span.setAttributes({
            [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
            [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
            [`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_VECTOR}`]:
              JSON.stringify(result.embedding),
            [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
          });
          span.setStatus({ code: SpanStatusCode.OK });
          span.end();
          return result;
        });

        return context.bind(execContext, wrappedPromise) as Promise<OllamaEmbeddingsResponse>;
      };
    };
  }

  /**
   * Un-patches the Ollama module
   */
  private unpatch(
    moduleExports: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    const Ollama = moduleExports.Ollama as {
      prototype: Record<string, unknown>;
    } | undefined;

    if (Ollama?.prototype) {
      if (typeof Ollama.prototype.chat === "function") {
        this._unwrap(Ollama.prototype, "chat");
      }
      if (typeof Ollama.prototype.generate === "function") {
        this._unwrap(Ollama.prototype, "generate");
      }
      if (typeof Ollama.prototype.embed === "function") {
        this._unwrap(Ollama.prototype, "embed");
      }
      if (typeof Ollama.prototype.embeddings === "function") {
        this._unwrap(Ollama.prototype, "embeddings");
      }
    }

    const defaultOllama = moduleExports.default as Record<string, unknown> | undefined;
    if (defaultOllama) {
      if (typeof defaultOllama.chat === "function") {
        this._unwrap(defaultOllama, "chat");
      }
      if (typeof defaultOllama.generate === "function") {
        this._unwrap(defaultOllama, "generate");
      }
      if (typeof defaultOllama.embed === "function") {
        this._unwrap(defaultOllama, "embed");
      }
      if (typeof defaultOllama.embeddings === "function") {
        this._unwrap(defaultOllama, "embeddings");
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
 * Type guard for chat response (non-streaming)
 */
function isChatResponse(
  response: OllamaChatResponse | OllamaAbortableAsyncIterator<OllamaChatResponse>,
): response is OllamaChatResponse {
  return "message" in response && "done" in response;
}

/**
 * Type guard for generate response (non-streaming)
 */
function isGenerateResponse(
  response: OllamaGenerateResponse | OllamaAbortableAsyncIterator<OllamaGenerateResponse>,
): response is OllamaGenerateResponse {
  return "response" in response && "done" in response;
}

/**
 * Type guard for async iterator (streaming)
 */
function isAsyncIterator<T>(
  response: T | OllamaAbortableAsyncIterator<T>,
): response is OllamaAbortableAsyncIterator<T> {
  return typeof (response as OllamaAbortableAsyncIterator<T>)[Symbol.asyncIterator] === "function";
}

/**
 * Wraps a chat stream to capture the full response
 */
async function* wrapChatStream(
  stream: OllamaAbortableAsyncIterator<OllamaChatResponse>,
  span: Span,
): AsyncGenerator<OllamaChatResponse, void, unknown> {
  let fullContent = "";
  let lastResponse: OllamaChatResponse | null = null;
  const allChunks: OllamaChatResponse[] = [];

  try {
    for await (const chunk of stream) {
      allChunks.push(chunk);
      if (chunk.message?.content) {
        fullContent += chunk.message.content;
      }
      lastResponse = chunk;
      yield chunk;
    }
  } catch (error) {
    span.recordException(error as Error);
    span.setStatus({ code: SpanStatusCode.ERROR, message: (error as Error).message });
    span.end();
    throw error;
  }

  const messageIndexPrefix = `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.`;
  const attributes: Attributes = {
    [SemanticConventions.OUTPUT_VALUE]: fullContent,
    [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
    [`${messageIndexPrefix}${SemanticConventions.MESSAGE_CONTENT}`]: fullContent,
    [`${messageIndexPrefix}${SemanticConventions.MESSAGE_ROLE}`]: "assistant",
    [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(allChunks) ?? "",
  };

  if (lastResponse) {
    attributes[SemanticConventions.LLM_MODEL_NAME] = lastResponse.model;
    if (lastResponse.prompt_eval_count !== undefined) {
      attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT] = lastResponse.prompt_eval_count;
    }
    if (lastResponse.eval_count !== undefined) {
      attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION] = lastResponse.eval_count;
    }
    if (lastResponse.prompt_eval_count !== undefined && lastResponse.eval_count !== undefined) {
      attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL] =
        lastResponse.prompt_eval_count + lastResponse.eval_count;
    }
  }

  span.setAttributes(attributes);
  span.setStatus({ code: SpanStatusCode.OK });
  span.end();
}

/**
 * Wraps a generate stream to capture the full response
 */
async function* wrapGenerateStream(
  stream: OllamaAbortableAsyncIterator<OllamaGenerateResponse>,
  span: Span,
): AsyncGenerator<OllamaGenerateResponse, void, unknown> {
  let fullResponse = "";
  let lastChunk: OllamaGenerateResponse | null = null;
  const allChunks: OllamaGenerateResponse[] = [];

  try {
    for await (const chunk of stream) {
      allChunks.push(chunk);
      if (chunk.response) {
        fullResponse += chunk.response;
      }
      lastChunk = chunk;
      yield chunk;
    }
  } catch (error) {
    span.recordException(error as Error);
    span.setStatus({ code: SpanStatusCode.ERROR, message: (error as Error).message });
    span.end();
    throw error;
  }

  const attributes: Attributes = {
    [SemanticConventions.OUTPUT_VALUE]: fullResponse,
    [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
    [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(allChunks) ?? "",
  };

  if (lastChunk) {
    attributes[SemanticConventions.LLM_MODEL_NAME] = lastChunk.model;
    if (lastChunk.prompt_eval_count !== undefined) {
      attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT] = lastChunk.prompt_eval_count;
    }
    if (lastChunk.eval_count !== undefined) {
      attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION] = lastChunk.eval_count;
    }
    if (lastChunk.prompt_eval_count !== undefined && lastChunk.eval_count !== undefined) {
      attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL] =
        lastChunk.prompt_eval_count + lastChunk.eval_count;
    }
  }

  span.setAttributes(attributes);
  span.setStatus({ code: SpanStatusCode.OK });
  span.end();
}

/**
 * Gets input message attributes for chat requests
 */
function getChatInputMessagesAttributes(request: OllamaChatRequest): Attributes {
  return request.messages.reduce((acc, message, index) => {
    const indexPrefix = `${SemanticConventions.LLM_INPUT_MESSAGES}.${index}.`;
    acc[`${indexPrefix}${SemanticConventions.MESSAGE_ROLE}`] = message.role;
    if (message.content) {
      acc[`${indexPrefix}${SemanticConventions.MESSAGE_CONTENT}`] = message.content;
    }
    return acc;
  }, {} as Attributes);
}

/**
 * Gets tool schema attributes for chat requests
 */
function getChatToolsJSONSchema(request: OllamaChatRequest): Attributes {
  if (!request.tools) {
    return {};
  }
  return request.tools.reduce((acc: Attributes, tool, index) => {
    const toolJsonSchema = safelyJSONStringify(tool);
    const key = `${SemanticConventions.LLM_TOOLS}.${index}.${SemanticConventions.TOOL_JSON_SCHEMA}`;
    if (toolJsonSchema) {
      acc[key] = toolJsonSchema;
    }
    return acc;
  }, {});
}

/**
 * Gets output message attributes from chat response
 */
function getChatOutputMessagesAttributes(response: OllamaChatResponse): Attributes {
  const indexPrefix = `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.`;
  const attributes: Attributes = {
    [`${indexPrefix}${SemanticConventions.MESSAGE_ROLE}`]: response.message.role,
  };

  if (response.message.content) {
    attributes[`${indexPrefix}${SemanticConventions.MESSAGE_CONTENT}`] = response.message.content;
  }

  if (response.message.tool_calls) {
    response.message.tool_calls.forEach((toolCall, index) => {
      const toolCallIndexPrefix = `${indexPrefix}${SemanticConventions.MESSAGE_TOOL_CALLS}.${index}.`;
      attributes[`${toolCallIndexPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_NAME}`] =
        toolCall.function.name;
      attributes[`${toolCallIndexPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}`] =
        JSON.stringify(toolCall.function.arguments);
    });
  }

  return attributes;
}

/**
 * Gets usage attributes from chat response
 */
function getChatUsageAttributes(response: OllamaChatResponse): Attributes {
  const attributes: Attributes = {};

  if (response.prompt_eval_count !== undefined) {
    attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT] = response.prompt_eval_count;
  }
  if (response.eval_count !== undefined) {
    attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION] = response.eval_count;
  }
  if (response.prompt_eval_count !== undefined && response.eval_count !== undefined) {
    attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL] =
      response.prompt_eval_count + response.eval_count;
  }

  return attributes;
}

/**
 * Gets input attributes for generate requests
 */
function getGenerateInputAttributes(request: OllamaGenerateRequest): Attributes {
  const indexPrefix = `${SemanticConventions.LLM_INPUT_MESSAGES}.0.`;
  const attributes: Attributes = {
    [`${indexPrefix}${SemanticConventions.MESSAGE_ROLE}`]: "user",
    [`${indexPrefix}${SemanticConventions.MESSAGE_CONTENT}`]: request.prompt,
  };

  if (request.system) {
    const systemPrefix = `${SemanticConventions.LLM_INPUT_MESSAGES}.1.`;
    attributes[`${systemPrefix}${SemanticConventions.MESSAGE_ROLE}`] = "system";
    attributes[`${systemPrefix}${SemanticConventions.MESSAGE_CONTENT}`] = request.system;
  }

  return attributes;
}

/**
 * Gets usage attributes from generate response
 */
function getGenerateUsageAttributes(response: OllamaGenerateResponse): Attributes {
  const attributes: Attributes = {};

  if (response.prompt_eval_count !== undefined) {
    attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT] = response.prompt_eval_count;
  }
  if (response.eval_count !== undefined) {
    attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION] = response.eval_count;
  }
  if (response.prompt_eval_count !== undefined && response.eval_count !== undefined) {
    attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL] =
      response.prompt_eval_count + response.eval_count;
  }

  return attributes;
}

/**
 * Gets input attributes for embed requests
 */
function getEmbedInputAttributes(texts: string[]): Attributes {
  return texts.reduce((acc: Attributes, text, index) => {
    acc[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.${index}.${SemanticConventions.EMBEDDING_TEXT}`] = text;
    return acc;
  }, {});
}

/**
 * Gets output attributes from embed response
 */
function getEmbedOutputAttributes(response: OllamaEmbedResponse): Attributes {
  const attributes: Attributes = {};

  response.embeddings.forEach((embedding, index) => {
    attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.${index}.${SemanticConventions.EMBEDDING_VECTOR}`] =
      JSON.stringify(embedding);
  });

  if (response.prompt_eval_count !== undefined) {
    attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT] = response.prompt_eval_count;
  }

  return attributes;
}
