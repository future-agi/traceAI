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
  thinking?: string;
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
  thinking?: string;
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

interface OllamaAbortableAsyncIterator<T> extends AsyncIterable<T> {
  // The supported 0.4.x SDK returns an async generator without abort().
  abort?(): void;
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
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.OLLAMA,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
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
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
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
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.OLLAMA,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "text_completion",
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
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
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
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.OLLAMA,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "embeddings",
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
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
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
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.OLLAMA,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "embeddings",
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
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
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
 * Observe the native iterator without replacing its abortable stream object.
 * Explicit controls also finalize a stream closed before its first next(), when
 * an async generator's finally block would never run. Cancellation aborts the
 * transport before awaiting return/throw, so a pending read cannot block cleanup.
 */
function wrapStream<T>(
  stream: OllamaAbortableAsyncIterator<T>,
  span: Span,
  onChunk: (chunk: T) => void,
  getAttributes: () => Attributes,
): OllamaAbortableAsyncIterator<T> {
  const iterator = stream[Symbol.asyncIterator]();
  let ended = false;
  type Outcome = { code: SpanStatusCode; error?: unknown };
  let cancellation: Outcome | undefined;
  let restoreAbort = () => {};

  function finish(outcome: Outcome) {
    if (ended) return;
    ended = true;
    try {
      span.setAttributes(getAttributes());
      if (outcome.code === SpanStatusCode.ERROR) {
        const error = outcome.error instanceof Error
          ? outcome.error : new Error(String(outcome.error));
        span.recordException(error);
        span.setStatus({ code: outcome.code, message: error.message });
      } else {
        span.setStatus({ code: outcome.code });
      }
    } catch {
      // Telemetry must not replace the SDK's result or original exception.
      diag.warn("Failed to record Ollama stream attributes");
    } finally {
      restoreAbort();
      span.end();
    }
  }

  const tracked: AsyncIterableIterator<T> = {
    [Symbol.asyncIterator]() { return this; },
    async next(...args: [] | [unknown]) {
      try {
        const result = await iterator.next(...args);
        if (result.done) {
          finish(cancellation ?? { code: SpanStatusCode.OK });
        } else if (!ended) {
          onChunk(result.value);
        }
        return result;
      } catch (error) {
        finish(cancellation ?? { code: SpanStatusCode.ERROR, error });
        throw error;
      }
    },
    async return(value?: unknown) {
      cancellation ??= { code: SpanStatusCode.UNSET };
      try {
        stream.abort?.();
        return iterator.return
          ? await iterator.return(value) : { done: true as const, value };
      } catch (error) {
        finish({ code: SpanStatusCode.ERROR, error });
        throw error;
      } finally {
        finish(cancellation);
      }
    },
    async throw(error?: unknown) {
      cancellation ??= { code: SpanStatusCode.ERROR, error };
      try {
        stream.abort?.();
        if (iterator.throw) return await iterator.throw(error);
        if (iterator.return) await iterator.return();
        throw error;
      } finally {
        finish(cancellation);
      }
    },
  };

  // The SDK client keeps the original stream in its active-request list. Observe
  // that object's abort as well, so client.abort() closes even an unread span.
  // Restore it on completion so the SDK cannot retain our captured output.
  const originalAbort = stream.abort;
  if (originalAbort) {
    const ownAbort = Object.prototype.hasOwnProperty.call(stream, "abort");
    const abort = () => {
      cancellation ??= { code: SpanStatusCode.UNSET };
      try {
        return originalAbort.call(stream);
      } finally {
        finish(cancellation);
      }
    };
    stream.abort = abort;
    restoreAbort = () => {
      if (stream.abort !== abort) return;
      if (ownAbort) stream.abort = originalAbort;
      else delete stream.abort;
    };
  }

  return new Proxy(stream, {
    get(target, property) {
      if (property === Symbol.asyncIterator) return () => tracked;
      // Keep the direct controls provided by earlier instrumentor releases, too.
      if (property === "next") return tracked.next;
      if (property === "return") return tracked.return;
      if (property === "throw") return tracked.throw;
      const value = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

/** Capture chat content, thinking and complete native tool calls across chunks. */
function wrapChatStream(
  stream: OllamaAbortableAsyncIterator<OllamaChatResponse>,
  span: Span,
): OllamaAbortableAsyncIterator<OllamaChatResponse> {
  const message: OllamaMessage = { role: "assistant", content: "" };
  let lastResponse: OllamaChatResponse | undefined;
  const allChunks: OllamaChatResponse[] = [];

  return wrapStream(stream, span, (chunk) => {
    allChunks.push(chunk);
    message.content += chunk.message?.content ?? "";
    if (chunk.message?.thinking) {
      message.thinking = (message.thinking ?? "") + chunk.message.thinking;
    }
    if (chunk.message?.tool_calls?.length) {
      (message.tool_calls ??= []).push(...chunk.message.tool_calls);
    }
    lastResponse = chunk;
  }, () => ({
    [SemanticConventions.OUTPUT_VALUE]: message.content,
    [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
    ...getChatOutputMessagesAttributes({ message }),
    [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(allChunks) ?? "",
    ...(lastResponse ? {
      [SemanticConventions.LLM_MODEL_NAME]: lastResponse.model,
      ...getChatUsageAttributes(lastResponse),
    } : {}),
  }));
}

/** Capture generate content and thinking without changing yielded chunks. */
function wrapGenerateStream(
  stream: OllamaAbortableAsyncIterator<OllamaGenerateResponse>,
  span: Span,
): OllamaAbortableAsyncIterator<OllamaGenerateResponse> {
  const message: OllamaMessage = { role: "assistant", content: "" };
  let lastChunk: OllamaGenerateResponse | undefined;
  const allChunks: OllamaGenerateResponse[] = [];

  return wrapStream(stream, span, (chunk) => {
    allChunks.push(chunk);
    message.content += chunk.response ?? "";
    if (chunk.thinking) {
      message.thinking = (message.thinking ?? "") + chunk.thinking;
    }
    lastChunk = chunk;
  }, () => ({
    [SemanticConventions.OUTPUT_VALUE]: message.content,
    [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
    ...getChatOutputMessagesAttributes({ message }),
    [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(allChunks) ?? "",
    ...(lastChunk ? {
      [SemanticConventions.LLM_MODEL_NAME]: lastChunk.model,
      ...getGenerateUsageAttributes(lastChunk),
    } : {}),
  }));
}

/**
 * Gets input message attributes for chat requests
 */
function getChatInputMessagesAttributes(request: OllamaChatRequest): Attributes {
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
 * Gets tool schema attributes for chat requests
 */
function getChatToolsJSONSchema(request: OllamaChatRequest): Attributes {
  if (!request.tools) {
    return {};
  }
  return { [SemanticConventions.LLM_TOOLS]: safelyJSONStringify(request.tools) ?? "[]" };
}

/**
 * Gets output message attributes from chat response
 */
function getChatOutputMessagesAttributes(response: Pick<OllamaChatResponse, "message">): Attributes {
  const msg: Record<string, unknown> = { role: response.message.role };
  if (response.message.content) {
    msg.content = response.message.content;
  }
  if (response.message.thinking) {
    msg.thinking = response.message.thinking;
  }
  if (response.message.tool_calls) {
    msg.tool_calls = response.message.tool_calls.map((tc) => ({
      function: { name: tc.function.name, arguments: JSON.stringify(tc.function.arguments) },
    }));
  }
  return { [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([msg]) ?? "[]" };
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
  const messages: Record<string, unknown>[] = [
    { role: "user", content: request.prompt },
  ];
  if (request.system) {
    messages.push({ role: "system", content: request.system });
  }
  return { [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(messages) ?? "[]" };
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
