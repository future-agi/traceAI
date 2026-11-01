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
  generateTraceConfig,
} from "@traceai/fi-core";

const MODULE_NAME = "openai";

/**
 * Flag to check if the xAI module has been patched
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

// Type definitions for OpenAI-compatible API (xAI/Grok)
interface ChatMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
}

interface ToolCall {
  id: string;
  type: "function";
  function: {
    name: string;
    arguments: string;
  };
}

interface Tool {
  type: "function";
  function: {
    name: string;
    description?: string;
    parameters?: Record<string, unknown>;
  };
}

interface ChatCompletionRequest {
  model: string;
  messages: ChatMessage[];
  max_tokens?: number;
  temperature?: number;
  top_p?: number;
  stop?: string | string[];
  stream?: boolean;
  tools?: Tool[];
  tool_choice?: string | { type: string; function: { name: string } };
  [key: string]: unknown;
}

interface ChatCompletionChoice {
  index: number;
  message: ChatMessage;
  finish_reason: string | null;
  delta?: {
    content?: string;
    role?: string;
    tool_calls?: Array<{
      index: number;
      id?: string;
      type?: string;
      function?: { name?: string; arguments?: string };
    }>;
  };
}

interface ChatCompletion {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: ChatCompletionChoice[];
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

interface EmbeddingRequest {
  model: string;
  input: string | string[];
}

interface EmbeddingData {
  object: string;
  embedding: number[];
  index: number;
}

interface EmbeddingResponse {
  object: string;
  data: EmbeddingData[];
  model: string;
  usage?: {
    prompt_tokens: number;
    total_tokens: number;
  };
}

interface StreamResponse<T> {
  [Symbol.asyncIterator](): AsyncIterator<T>;
}

/** Keep the SDK's APIPromise intact: its response helpers and _thenUnwrap are
 * used by higher-level resource methods (including structured parse). Both
 * v4 and v7 expose parseResponse on each instance, but accept different
 * arguments, so forward them without interpreting them here. */
function traceSDKPromise<T>(
  result: Promise<T>,
  span: Span,
  execContext: ReturnType<typeof context.active>,
  onValue: (value: T) => T,
): Promise<T> {
  let finished = false;
  const fail = (error: Error) => {
    if (!finished) {
      finished = true;
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
      span.end();
    }
  };
  const sdkPromise = result as Promise<T> & {
    parseResponse?: (...args: unknown[]) => Promise<T>;
    responsePromise?: Promise<unknown>;
    parsedPromise?: Promise<T>;
    asResponse?: () => Promise<Response>;
  };
  if (typeof sdkPromise.parseResponse === "function" && sdkPromise.responsePromise) {
    const originalParse = sdkPromise.parseResponse;
    sdkPromise.parseResponse = async (...args: unknown[]) => {
      try {
        const value = await originalParse(...args);
        return context.with(execContext, () => {
          const transformed = onValue(value);
          if (!isStreamResponse(transformed)) finished = true;
          return transformed;
        });
      } catch (error) {
        fail(error as Error);
        throw error;
      }
    };
    // asResponse() bypasses parsing; finish its span if no parse was requested.
    // withResponse() starts parsing first, so it still records the response data.
    if (typeof sdkPromise.asResponse === "function") {
      const originalAsResponse = sdkPromise.asResponse;
      sdkPromise.asResponse = () => originalAsResponse.call(sdkPromise).then(response => {
        if (!sdkPromise.parsedPromise && !finished) {
          finished = true;
          span.setStatus({ code: SpanStatusCode.OK });
          span.end();
        }
        return response;
      });
    }
    // A network rejection also needs to finish when only asResponse() is used.
    void sdkPromise.responsePromise.catch(fail);
    return sdkPromise;
  }
  return context.bind(execContext, result.then(value => context.with(execContext, () => onValue(value))).catch(error => {
    fail(error);
    throw error;
  }));
}

/**
 * @param instrumentationConfig The config for the instrumentation
 * @param traceConfig The FI trace configuration for masking sensitive information
 */
export class XAIInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private hideInputs = false;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super(
      "@traceai/xai",
      VERSION,
      Object.assign({}, instrumentationConfig),
    );
    this._traceConfig = traceConfig;
  }

  public override enable(): void {
    super.enable();
    const traceConfig = generateTraceConfig(this._traceConfig);
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig });
    this.hideInputs = traceConfig.hideInputs;
  }

  public readonly instrumentationName = "@traceai/xai";

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "openai",
      [">=4.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  /**
   * Manually instruments the OpenAI module for xAI
   */
  manuallyInstrument(module: Record<string, unknown>) {
    this.patch(module);
  }

  /**
   * Checks if a request is targeting xAI API
   */
  private isXAIRequest(client: unknown): boolean {
    // v4 resources use client, v7 resources use _client. Direct wrapper
    // calls can also receive the parent OpenAI client itself.
    const resource = client as { client?: { baseURL?: string }; _client?: { baseURL?: string }; baseURL?: string } | null;
    const baseURL = resource?._client?.baseURL ?? resource?.client?.baseURL ?? resource?.baseURL;
    if (!baseURL) return false;
    try {
      const host = new URL(baseURL).hostname;
      return host === "x.ai" || host.endsWith(".x.ai");
    } catch {
      return false;
    }
  }

  /**
   * Patches the OpenAI module for xAI
   */
  private patch(
    module: Record<string, unknown> & { fiXAIPatched?: boolean },
    moduleVersion?: string,
  ) {
    if (module?.fiXAIPatched || _isFIPatched) {
      return module;
    }

    const instrumentation: XAIInstrumentation = this;

    // Ensure fiTracer uses the most up-to-date tracer
    if (!(instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer ||
        (instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer !== instrumentation.tracer) {
      const traceConfig = generateTraceConfig(instrumentation._traceConfig);
      instrumentation.fiTracer = new FITracer({
        tracer: instrumentation.tracer,
        traceConfig,
      });
      instrumentation.hideInputs = traceConfig.hideInputs;
    }

    // The SDK instantiates resource classes; chat and embeddings are not
    // properties on OpenAI.prototype (in either v4 or v7).
    const resources = this.getResourcePrototypes(module);
    if (!resources.chat && !resources.embeddings) {
      diag.warn("No OpenAI chat or embeddings resources available for xAI instrumentation");
      return module;
    }
    if (resources.chat) {
      this._wrap(resources.chat, "create", this.createChatCompletionWrapper(instrumentation));
    }
    if (resources.embeddings) {
      this._wrap(resources.embeddings, "create", this.createEmbeddingWrapper(instrumentation));
    }

    _isFIPatched = true;
    try {
      module.fiXAIPatched = true;
    } catch (e) {
      diag.warn(`Failed to set fiXAIPatched flag on module '${MODULE_NAME}'. Error: ${e}`);
    }

    return module;
  }

  /**
   * Creates a wrapper for chat.completions.create
   */
  private createChatCompletionWrapper(instrumentation: XAIInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedChatCreate(
        this: unknown,
        body: ChatCompletionRequest,
        options?: Record<string, unknown>,
      ) {
        if (!instrumentation.isXAIRequest(this)) {
          return original.apply(this, [body, options]);
        }

        const { messages: _messages, ...invocationParameters } = body;

        const span = instrumentation.fiTracer.startSpan(
          `xAI Chat Completions`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: body.model,
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(body),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                JSON.stringify(invocationParameters),
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.XAI,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
              ...getChatInputMessagesAttributes(body),
              ...getToolsJSONSchema(body),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(body) ?? "",
            },
          },
        );

        const execContext = getExecContext(span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [body, options]);
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

        return traceSDKPromise(execPromise as Promise<ChatCompletion | StreamResponse<ChatCompletion>>, span, execContext, (result) => {
          if (isChatCompletionResponse(result)) {
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
          } else if (isStreamResponse(result)) {
            return instrumentChatStream(result, span);
          }
          return result;
        });
      };
    };
  }

  /**
   * Creates a wrapper for embeddings.create
   */
  private createEmbeddingWrapper(instrumentation: XAIInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedEmbeddingCreate(
        this: unknown,
        body: EmbeddingRequest,
        options?: Record<string, unknown>,
      ) {
        if (!instrumentation.isXAIRequest(this)) {
          return original.apply(this, [body, options]);
        }

        const inputTexts = Array.isArray(body.input) ? body.input : [body.input];

        const span = instrumentation.fiTracer.startSpan(
          `xAI Embeddings`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.EMBEDDING,
              [SemanticConventions.EMBEDDING_MODEL_NAME]: body.model,
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(body),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.XAI,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "embeddings",
              ...(!instrumentation.hideInputs ? getEmbeddingInputAttributes(inputTexts) : {}),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(body) ?? "",
            },
          },
        );

        const execContext = getExecContext(span);
        const execPromise = safeExecuteInTheMiddle(
          () => {
            return context.with(trace.setSpan(execContext, span), () => {
              return original.apply(this, [body, options]);
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

        return traceSDKPromise(execPromise as Promise<EmbeddingResponse>, span, execContext, (result) => {
          span.setAttributes({
            [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
            [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
            [SemanticConventions.EMBEDDING_MODEL_NAME]: result.model,
            ...getEmbeddingOutputAttributes(result),
            [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
          });
          span.setStatus({ code: SpanStatusCode.OK });
          span.end();
          return result;
        });
      };
    };
  }

  private getResourcePrototypes(module: Record<string, unknown>): {
    chat?: Record<string, unknown>;
    embeddings?: Record<string, unknown>;
  } {
    const sdk = (module.OpenAI ?? module.default) as {
      Chat?: { Completions?: { prototype?: Record<string, unknown> } };
      Embeddings?: { prototype?: Record<string, unknown> };
    } | undefined;
    const chat = sdk?.Chat?.Completions?.prototype;
    const embeddings = sdk?.Embeddings?.prototype;
    return {
      chat: typeof chat?.create === "function" ? chat : undefined,
      embeddings: typeof embeddings?.create === "function" ? embeddings : undefined,
    };
  }

  /**
   * Un-patches the OpenAI module
   */
  private unpatch(
    moduleExports: Record<string, unknown> & { fiXAIPatched?: boolean },
    moduleVersion?: string,
  ) {
    const resources = this.getResourcePrototypes(moduleExports);
    if (resources.chat) this._unwrap(resources.chat, "create");
    if (resources.embeddings) this._unwrap(resources.embeddings, "create");

    _isFIPatched = false;
    try {
      moduleExports.fiXAIPatched = false;
    } catch (e) {
      diag.warn(`Failed to unset fiXAIPatched flag on module '${MODULE_NAME}'. Error: ${e}`);
    }
  }
}

/**
 * Type guard for chat completion response
 */
function isChatCompletionResponse(
  response: ChatCompletion | StreamResponse<ChatCompletion>,
): response is ChatCompletion {
  return "choices" in response && Array.isArray((response as ChatCompletion).choices);
}

/**
 * Type guard for stream response
 */
function isStreamResponse<T>(
  response: T | StreamResponse<T>,
): response is StreamResponse<T> {
  return typeof (response as StreamResponse<T>)[Symbol.asyncIterator] === "function";
}

/** Preserve the SDK Stream instance (and its controller, tee and readable helpers).
 * Both v4 and v7 route these helpers through the instance's iterator factory.
 * Replacing only that factory records whichever consumption path the caller uses.
 */
function instrumentChatStream<T extends StreamResponse<ChatCompletion>>(stream: T, span: Span): T {
  const sdkStream = stream as T & { iterator?: () => AsyncIterator<ChatCompletion> };
  if (typeof sdkStream.iterator === "function") {
    const originalIterator = sdkStream.iterator.bind(sdkStream);
    sdkStream.iterator = () => wrapChatStream({ [Symbol.asyncIterator]: originalIterator }, span);
  } else {
    const originalIterator = stream[Symbol.asyncIterator].bind(stream);
    stream[Symbol.asyncIterator] = () => wrapChatStream({ [Symbol.asyncIterator]: originalIterator }, span);
  }
  return stream;
}

/**
 * Wraps a chat stream to capture the full response
 */
async function* wrapChatStream(
  stream: StreamResponse<ChatCompletion>,
  span: Span,
): AsyncGenerator<ChatCompletion, void, unknown> {
  const allChunks: ChatCompletion[] = [];
  const messages = new Map<number, { role: string; content: string; tool_calls: Map<number, ToolCall> }>();
  const finishReasons = new Map<number, string>();
  let responseId: string | undefined;
  let responseModel: string | undefined;
  let usage: ChatCompletion["usage"];
  let failed = false;
  let completed = false;

  try {
    for await (const chunk of stream) {
      allChunks.push(chunk);
      responseId = chunk.id || responseId;
      responseModel = chunk.model || responseModel;
      usage = chunk.usage || usage;
      for (const choice of chunk.choices ?? []) {
        const message = messages.get(choice.index) ?? { role: "assistant", content: "", tool_calls: new Map<number, ToolCall>() };
        message.role = choice.delta?.role || message.role;
        message.content += choice.delta?.content ?? "";
        for (const fragment of choice.delta?.tool_calls ?? []) {
          const tool = message.tool_calls.get(fragment.index) ?? { id: "", type: "function" as const, function: { name: "", arguments: "" } };
          tool.id += fragment.id ?? "";
          tool.function.name += fragment.function?.name ?? "";
          tool.function.arguments += fragment.function?.arguments ?? "";
          message.tool_calls.set(fragment.index, tool);
        }
        messages.set(choice.index, message);
        if (choice.finish_reason) finishReasons.set(choice.index, choice.finish_reason);
      }
      yield chunk;
    }
    completed = true;
  } catch (error) {
    failed = true;
    span.recordException(error as Error);
    span.setStatus({ code: SpanStatusCode.ERROR, message: (error as Error).message });
    throw error;
  } finally {
    // Async-generator return() lands here too; for-await forwards it to the
    // original stream, preserving SDK cancellation while retaining partial data.
    const output = [...messages.entries()].sort(([a], [b]) => a - b).map(([, message]) => ({
      role: message.role,
      content: message.content,
      ...(message.tool_calls.size ? { tool_calls: [...message.tool_calls.entries()].sort(([a], [b]) => a - b).map(([, tool]) => tool) } : {}),
    }));
    const attributes: Attributes = {
      [SemanticConventions.OUTPUT_VALUE]: output.map(message => message.content).join(""),
      [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
      [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify(output) ?? "[]",
      [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(allChunks) ?? "",
    };
    if (responseId) attributes[SemanticConventions.GEN_AI_RESPONSE_ID] = responseId;
    if (responseModel) {
      attributes[SemanticConventions.LLM_MODEL_NAME] = responseModel;
      attributes[SemanticConventions.GEN_AI_RESPONSE_MODEL] = responseModel;
    }
    if (finishReasons.size) attributes[SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS] = safelyJSONStringify([...finishReasons.entries()].sort(([a], [b]) => a - b).map(([, reason]) => reason)) ?? "[]";
    if (usage) {
      attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT] = usage.prompt_tokens;
      attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION] = usage.completion_tokens;
      attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL] = usage.total_tokens;
    }
    span.setAttributes(attributes);
    if (!failed) span.setStatus({ code: completed ? SpanStatusCode.OK : SpanStatusCode.UNSET });
    span.end();
  }
}

/**
 * Gets input message attributes for chat requests
 */
function getChatInputMessagesAttributes(request: ChatCompletionRequest): Attributes {
  const serialized = request.messages.map((message) => {
    const obj: Record<string, unknown> = { role: message.role };
    if (message.content) {
      obj.content = message.content;
    }
    if (message.tool_call_id) {
      obj.tool_call_id = message.tool_call_id;
    }
    return obj;
  });
  return { [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(serialized) ?? "[]" };
}

/**
 * Gets tool schema attributes
 */
function getToolsJSONSchema(request: ChatCompletionRequest): Attributes {
  if (!request.tools) {
    return {};
  }
  return { [SemanticConventions.LLM_TOOLS]: safelyJSONStringify(request.tools) ?? "[]" };
}

/**
 * Gets output message attributes from chat response
 */
function getChatOutputMessagesAttributes(response: ChatCompletion): Attributes {
  const choice = response.choices[0];
  if (!choice) {
    return {};
  }

  const msg: Record<string, unknown> = { role: choice.message.role };
  if (choice.message.content) {
    msg.content = choice.message.content;
  }
  if (choice.message.tool_calls) {
    msg.tool_calls = choice.message.tool_calls.map((tc) => ({
      id: tc.id,
      function: { name: tc.function.name, arguments: tc.function.arguments },
    }));
  }
  return { [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([msg]) ?? "[]" };
}

/**
 * Gets usage attributes from chat response
 */
function getUsageAttributes(response: ChatCompletion): Attributes {
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
function getEmbeddingOutputAttributes(response: EmbeddingResponse): Attributes {
  const attributes: Attributes = {};

  response.data.forEach((item, index) => {
    attributes[`${SemanticConventions.EMBEDDING_EMBEDDINGS}.${index}.${SemanticConventions.EMBEDDING_VECTOR}`] =
      JSON.stringify(item.embedding);
  });

  if (response.usage) {
    attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT] = response.usage.prompt_tokens;
    attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL] = response.usage.total_tokens;
  }

  return attributes;
}
