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

// Identify our exact wrappers, including when inherited by the default singleton.
// This is not an admission flag: separate CJS/ESM constructors need separate patches.
const patchedFunctions = new WeakSet<object>();
let activePatchCount = 0;

interface MethodPatch {
  owner: Record<string, unknown>;
  name: string;
  wrapped: object;
}

/** Whether any Ollama methods are currently tracked as instrumented. */
export function isPatched() {
  return activePatchCount > 0;
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
  messages?: OllamaMessage[];
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
  // Lazily initialized: InstrumentationBase can patch already-loaded modules in super().
  private _modulePatches?: WeakMap<object, MethodPatch[]>;

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
      MODULE_NAME,
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
  private patch(module: Record<string, unknown>) {
    // A module namespace can be immutable. Keep ownership outside the SDK exports.
    this._modulePatches ??= new WeakMap();
    const patches = this._modulePatches.get(module) ?? [];
    this._modulePatches.set(module, patches);
    this.fiTracer = new FITracer({
      tracer: this.tracer,
      traceConfig: this._traceConfig,
    });

    const Ollama = module.Ollama as { prototype: Record<string, unknown> } | undefined;
    const defaultOllama = module.default as Record<string, unknown> | undefined;
    const wrappers = {
      chat: this.createChatWrapper(this),
      generate: this.createGenerateWrapper(this),
      embed: this.createEmbedWrapper(this),
      embeddings: this.createEmbeddingsWrapper(this),
    };
    for (const owner of [Ollama?.prototype, defaultOllama]) {
      if (!owner) continue;
      for (const [name, wrapper] of Object.entries(wrappers)) {
        const original = owner[name];
        if (typeof original !== "function" || patchedFunctions.has(original)) continue;
        // _wrap unwraps an already-wrapped inherited method before replacing it.
        // In particular, never rewrap the singleton's inherited class wrapper.
        this._wrap(owner, name, wrapper);
        const wrapped = owner[name];
        if (typeof wrapped === "function" && wrapped !== original) {
          patches.push({ owner, name, wrapped });
          patchedFunctions.add(wrapped);
          activePatchCount++;
        }
      }
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
        const invocationParameters = getInvocationParameters(request);

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
              // Tool definitions stay in INPUT_VALUE/RAW_INPUT, where FITracer
              // applies code and environment input-hiding controls.
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
        const invocationParameters = getInvocationParameters(request);

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
  private unpatch(moduleExports: Record<string, unknown>) {
    const patches = this._modulePatches?.get(moduleExports);
    if (!patches) return;
    const retained: MethodPatch[] = [];
    for (const patch of patches) {
      // A different instrumentor may have replaced/wrapped this function since
      // we installed it. Never invoke that instrumentor's __unwrap hook.
      if (patch.owner[patch.name] !== patch.wrapped) {
        retained.push(patch);
        continue;
      }
      this._unwrap(patch.owner, patch.name);
      patchedFunctions.delete(patch.wrapped);
      activePatchCount--;
    }
    if (retained.length) this._modulePatches?.set(moduleExports, retained);
    else this._modulePatches?.delete(moduleExports);
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
  const serialized = (request.messages ?? []).map((message) => {
    const obj: Record<string, unknown> = { role: message.role };
    if (message.content) {
      obj.content = message.content;
    }
    return obj;
  });
  return { [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(serialized) ?? "[]" };
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

// Content-bearing fields (including stop sequences, JSON schemas and context
// tokens) belong only in the privacy-governed input attributes, never parameters.
const INVOCATION_NUMERIC_OPTIONS = [
  "num_keep", "seed", "num_predict", "top_k", "top_p", "tfs_z", "typical_p",
  "repeat_last_n", "temperature", "repeat_penalty", "presence_penalty",
  "frequency_penalty", "mirostat", "mirostat_tau", "mirostat_eta", "num_ctx",
  "num_batch", "num_gpu", "main_gpu", "rope_frequency_base", "rope_frequency_scale",
  "num_thread",
] as const;
const INVOCATION_BOOLEAN_OPTIONS = [
  "penalize_newline", "numa", "low_vram", "f16_kv", "vocab_only", "use_mmap",
  "use_mlock", "embedding_only",
] as const;

function getInvocationParameters(request: OllamaChatRequest | OllamaGenerateRequest): Record<string, unknown> {
  const parameters: Record<string, unknown> = { model: request.model };
  if (typeof request.stream === "boolean") parameters.stream = request.stream;
  if ("raw" in request && typeof request.raw === "boolean") parameters.raw = request.raw;
  if (request.format === "json") parameters.format = request.format;
  const keepAlive = request.keep_alive;
  if ((typeof keepAlive === "number" && Number.isFinite(keepAlive)) ||
      (typeof keepAlive === "string" && /^-?\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h)?$/.test(keepAlive))) {
    parameters.keep_alive = keepAlive;
  }
  if (request.options) {
    const options: Record<string, number | boolean> = {};
    for (const key of INVOCATION_NUMERIC_OPTIONS) {
      const value = request.options[key];
      if (typeof value === "number" && Number.isFinite(value)) options[key] = value;
    }
    for (const key of INVOCATION_BOOLEAN_OPTIONS) {
      const value = request.options[key];
      if (typeof value === "boolean") options[key] = value;
    }
    parameters.options = options;
  }
  return parameters;
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
