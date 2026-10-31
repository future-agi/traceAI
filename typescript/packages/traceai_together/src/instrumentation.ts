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
  generateTraceConfig,
} from "@traceai/fi-core";

const MODULE_NAME = "together-ai";

// Track actual resource wrappers, not an optimistic flag on the module namespace.
const patchedResources = new Set<object>();
export function isPatched() {
  return patchedResources.size > 0;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Resource = { create: (...args: any[]) => any };
type ResourceClass = { prototype: Resource };
type TogetherExports = {
  Chat?: { Completions?: ResourceClass };
  Completions?: ResourceClass;
  Embeddings?: ResourceClass;
};

// Type definitions for Together AI SDK
interface TogetherMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  tool_calls?: TogetherToolCall[];
  tool_call_id?: string;
}

interface TogetherToolCall {
  id: string;
  type: "function";
  function: {
    name: string;
    arguments: string;
  };
}

interface TogetherTool {
  type: "function";
  function: {
    name: string;
    description?: string;
    parameters?: Record<string, unknown>;
  };
}

interface TogetherChatCompletionRequest {
  model: string;
  messages: TogetherMessage[];
  max_tokens?: number;
  temperature?: number;
  top_p?: number;
  top_k?: number;
  repetition_penalty?: number;
  stop?: string | string[];
  stream?: boolean;
  tools?: TogetherTool[];
  tool_choice?: string | { type: string; function: { name: string } };
  response_format?: { type: string };
  [key: string]: unknown;
}

interface TogetherChatCompletionChoice {
  index: number;
  message: TogetherMessage;
  finish_reason: string;
  delta?: {
    content?: string;
    role?: string;
    tool_calls?: TogetherToolCall[];
  };
}

interface TogetherChatCompletion {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: TogetherChatCompletionChoice[];
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

interface TogetherCompletionRequest {
  model: string;
  prompt: string;
  max_tokens?: number;
  temperature?: number;
  top_p?: number;
  top_k?: number;
  repetition_penalty?: number;
  stop?: string | string[];
  stream?: boolean;
  [key: string]: unknown;
}

interface TogetherCompletionChoice {
  text: string;
  index: number;
  finish_reason: string;
}

interface TogetherCompletion {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: TogetherCompletionChoice[];
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

interface TogetherEmbeddingRequest {
  model: string;
  input: string | string[];
}

interface TogetherEmbeddingData {
  object: string;
  embedding: number[];
  index: number;
}

interface TogetherEmbeddingResponse {
  object: string;
  data: TogetherEmbeddingData[];
  model: string;
  usage?: {
    prompt_tokens: number;
    total_tokens: number;
  };
}

interface TogetherStream<T> {
  [Symbol.asyncIterator](): AsyncIterator<T>;
  // The SDK's tee() reads this factory directly in both 0.6.x and 1.x.
  iterator?: () => AsyncIterator<T>;
}

/**
 * @param instrumentationConfig The config for the instrumentation
 * @param traceConfig The FI trace configuration for masking sensitive information
 */
export class TogetherInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  private readonly _resources = new Set<Resource>();
  private readonly _manualModules = new Set<Record<string, unknown>>();

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super(
      "@traceai/together",
      VERSION,
      { ...instrumentationConfig, enabled: false },
    );
    // InstrumentationBase enables before subclass fields exist; enable once they do.
    this.setConfig({ ...instrumentationConfig });
    this._traceConfig = generateTraceConfig(traceConfig);
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
    if (this.getConfig().enabled) this.enable();
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
    for (const module of this._manualModules) this.patch(module);
  }

  public override setTracerProvider(provider: TracerProvider): void {
    super.setTracerProvider(provider);
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  public override disable(): void {
    super.disable();
    for (const resource of this._resources) {
      this._unwrap(resource, "create");
      patchedResources.delete(resource);
    }
    this._resources.clear();
  }

  public readonly instrumentationName = "@traceai/together";

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "together-ai",
      [">=0.6.0 <2"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  /**
   * Manually instruments the Together module
   */
  manuallyInstrument(module: Record<string, unknown>) {
    this._manualModules.add(module);
    if (this.isEnabled()) this.patch(module);
  }

  /**
   * Patches the Together module
   */
  private patch(
    module: Record<string, unknown>,
  ) {
    const Together = (module.Together ?? module.default ?? module) as TogetherExports;
    const candidates = [
      [Together.Chat?.Completions, this.createChatCompletionWrapper(this)],
      [Together.Completions, this.createCompletionWrapper(this)],
      [Together.Embeddings, this.createEmbeddingWrapper(this)],
    ] as const;
    let available = 0;
    for (const [ResourceConstructor, wrapper] of candidates) {
      const resource = ResourceConstructor?.prototype;
      if (!resource || typeof resource.create !== "function") continue;
      available++;
      // A second instrumentor or a CJS/ESM alias must not double-wrap the same resource.
      if (patchedResources.has(resource)) continue;
      this._wrap(resource, "create", wrapper);
      this._resources.add(resource);
      patchedResources.add(resource);
    }
    if (available !== candidates.length) {
      diag.warn(
        `${MODULE_NAME}: found ${available}/${candidates.length} supported resource hooks; ` +
        "expected together-ai >=0.6.0 <2 (Chat.Completions, Completions, Embeddings).",
      );
    }

    return module;
  }

  /**
   * Creates a wrapper for chat.completions.create
   */
  private createChatCompletionWrapper(instrumentation: TogetherInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedChatCreate(
        this: unknown,
        body: TogetherChatCompletionRequest,
        options?: Record<string, unknown>,
      ) {
        if (isTracingSuppressed(context.active())) return original.apply(this, [body, options]);
        const { messages: _messages, ...invocationParameters } = body;

        const span = instrumentation.fiTracer.startSpan(
          `Together Chat Completions`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: body.model,
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(body),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                getInvocationParameters(invocationParameters, instrumentation._traceConfig?.hideInputs),
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.TOGETHER,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
              ...getChatInputMessagesAttributes(body),
              ...(instrumentation._traceConfig?.hideInputs ? {} : getToolsJSONSchema(body)),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(body) ?? "",
            },
          },
        );

        const execContext = context.active();
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

        return observeResponse(execPromise as Promise<TogetherChatCompletion | TogetherStream<TogetherChatCompletion>>, span, (result) => {
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
            return wrapChatStream(result, span);
          }
          return result;
        });
      };
    };
  }

  /**
   * Creates a wrapper for completions.create
   */
  private createCompletionWrapper(instrumentation: TogetherInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedCompletionCreate(
        this: unknown,
        body: TogetherCompletionRequest,
        options?: Record<string, unknown>,
      ) {
        if (isTracingSuppressed(context.active())) return original.apply(this, [body, options]);
        const { prompt: _prompt, ...invocationParameters } = body;

        const span = instrumentation.fiTracer.startSpan(
          `Together Completions`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: body.model,
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(body),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                getInvocationParameters(invocationParameters, instrumentation._traceConfig?.hideInputs),
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.TOGETHER,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "text_completion",
              ...(instrumentation._traceConfig?.hideInputs ? {} : {
                [`${SemanticConventions.LLM_PROMPTS}.0`]: body.prompt,
              }),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(body) ?? "",
            },
          },
        );

        const execContext = context.active();
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

        return observeResponse(execPromise as Promise<TogetherCompletion | TogetherStream<TogetherCompletion>>, span, (result) => {
          if (isStreamResponse(result)) return wrapCompletionStream(result, span);
          span.setAttributes({
            [SemanticConventions.OUTPUT_VALUE]: result.choices[0]?.text ?? "",
            [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
            [SemanticConventions.LLM_MODEL_NAME]: result.model,
            ...getCompletionUsageAttributes(result),
            [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
          });
          span.setStatus({ code: SpanStatusCode.OK });
          span.end();
          return result;
        });
      };
    };
  }

  /**
   * Creates a wrapper for embeddings.create
   */
  private createEmbeddingWrapper(instrumentation: TogetherInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedEmbeddingCreate(
        this: unknown,
        body: TogetherEmbeddingRequest,
        options?: Record<string, unknown>,
      ) {
        if (isTracingSuppressed(context.active())) return original.apply(this, [body, options]);
        const inputTexts = Array.isArray(body.input) ? body.input : [body.input];

        const span = instrumentation.fiTracer.startSpan(
          `Together Embeddings`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.EMBEDDING,
              [SemanticConventions.EMBEDDING_MODEL_NAME]: body.model,
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(body),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.TOGETHER,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "embeddings",
              ...(instrumentation._traceConfig?.hideInputs ? {} : getEmbeddingInputAttributes(inputTexts)),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(body) ?? "",
            },
          },
        );

        const execContext = context.active();
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

        return observeResponse(execPromise as Promise<TogetherEmbeddingResponse>, span, (result) => {
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

  /**
   * Un-patches the Together module
   */
  private unpatch(
    moduleExports: Record<string, unknown>,
  ) {
    const Together = (moduleExports.Together ?? moduleExports.default ?? moduleExports) as TogetherExports;
    for (const constructor of [Together.Chat?.Completions, Together.Completions, Together.Embeddings]) {
      const resource = constructor?.prototype;
      if (resource && this._resources.delete(resource)) {
        this._unwrap(resource, "create");
        patchedResources.delete(resource);
      }
    }
  }
}

/**
 * Type guard for chat completion response
 */
function isChatCompletionResponse(
  response: TogetherChatCompletion | TogetherStream<TogetherChatCompletion>,
): response is TogetherChatCompletion {
  return "choices" in response && Array.isArray((response as TogetherChatCompletion).choices);
}

/**
 * Type guard for stream response
 */
function isStreamResponse<T>(
  response: T | TogetherStream<T>,
): response is TogetherStream<T> {
  return typeof (response as TogetherStream<T>)[Symbol.asyncIterator] === "function";
}

/**
 * Observe the SDK's public promise APIs lazily. In particular, asResponse() must
 * not race a hidden JSON/stream consumer or lose access to the raw response body.
 */
function observeResponse<T>(promise: Promise<T>, span: Span, onResult: (result: T) => T): Promise<T> {
  let parsed: Promise<T> | undefined;
  const fail = (error: Error): never => {
    span.recordException(error);
    span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
    span.end();
    throw error;
  };
  const parse = () => {
    parsed ??= promise.then(onResult).catch(fail);
    return parsed;
  };
  return new Proxy(promise, {
    get(target, property) {
      if (property === "then") return (...args: Parameters<Promise<T>["then"]>) => parse().then(...args);
      if (property === "catch") return (...args: Parameters<Promise<T>["catch"]>) => parse().catch(...args);
      if (property === "finally") return (...args: Parameters<Promise<T>["finally"]>) => parse().finally(...args);
      const value = Reflect.get(target, property, target);
      if (property === "withResponse" && typeof value === "function") {
        return async () => {
          // Use the SDK envelope, including any version-specific metadata.
          try {
            const response = await value.call(target);
            parsed ??= Promise.resolve(response.data).then(onResult).catch(fail);
            return { ...response, data: await parsed };
          } catch (error) {
            if (parsed) throw error;
            return fail(error as Error);
          }
        };
      }
      if (property === "asResponse" && typeof value === "function") {
        return () => value.call(target).then((response: unknown) => {
          span.setStatus({ code: SpanStatusCode.OK });
          span.end();
          return response;
        }, fail);
      }
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

/** Decorate iteration in place so controller, tee() and toReadableStream() survive. */
function observeStream<T>(stream: TogetherStream<T>, span: Span, attributes: (chunk: T) => Attributes): TogetherStream<T> {
  const iterate = typeof stream.iterator === "function"
    ? stream.iterator.bind(stream)
    : stream[Symbol.asyncIterator].bind(stream);
  const observed = async function* () {
    let failed = false;
    try {
      for await (const chunk of { [Symbol.asyncIterator]: iterate }) {
        span.setAttributes(attributes(chunk));
        yield chunk;
      }
    } catch (error) {
      failed = true;
      span.recordException(error as Error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: (error as Error).message });
      throw error;
    } finally {
      if (!failed) span.setStatus({ code: SpanStatusCode.OK });
      span.end();
    }
  };
  if (typeof stream.iterator === "function") stream.iterator = observed;
  else stream[Symbol.asyncIterator] = observed;
  return stream;
}

function wrapChatStream(stream: TogetherStream<TogetherChatCompletion>, span: Span): TogetherStream<TogetherChatCompletion> {
  let content = "";
  const tools = new Map<number, TogetherToolCall>();
  return observeStream(stream, span, (chunk) => {
    const choice = chunk.choices?.[0];
    content += choice?.delta?.content ?? "";
    for (const delta of choice?.delta?.tool_calls ?? []) {
      const index = (delta as TogetherToolCall & { index?: number }).index ?? 0;
      const tool = tools.get(index) ?? { id: "", type: "function", function: { name: "", arguments: "" } };
      if (delta.id) tool.id = delta.id;
      tool.function.name += delta.function?.name ?? "";
      tool.function.arguments += delta.function?.arguments ?? "";
      tools.set(index, tool);
    }
    const message = { role: "assistant", content, ...(tools.size ? { tool_calls: [...tools.values()] } : {}) };
    return {
      [SemanticConventions.OUTPUT_VALUE]: content,
      [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
      [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([message]) ?? "[]",
      // Aggregate the result, rather than retaining every chunk in memory.
      [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(message) ?? "",
      [SemanticConventions.LLM_MODEL_NAME]: chunk.model,
      [SemanticConventions.GEN_AI_RESPONSE_MODEL]: chunk.model,
      [SemanticConventions.GEN_AI_RESPONSE_ID]: chunk.id,
      ...(choice?.finish_reason ? {
        [SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS]: JSON.stringify([choice.finish_reason]),
      } : {}),
      ...getUsageAttributes(chunk),
    };
  });
}

function wrapCompletionStream(stream: TogetherStream<TogetherCompletion>, span: Span): TogetherStream<TogetherCompletion> {
  let content = "";
  return observeStream(stream, span, (chunk) => {
    content += chunk.choices?.[0]?.text ?? "";
    return {
      [SemanticConventions.OUTPUT_VALUE]: content,
      [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
      [SemanticConventions.RAW_OUTPUT]: content,
      [SemanticConventions.LLM_MODEL_NAME]: chunk.model,
      ...getCompletionUsageAttributes(chunk),
    };
  });
}

/**
 * Gets input message attributes for chat requests
 */
function getChatInputMessagesAttributes(request: TogetherChatCompletionRequest): Attributes {
  const serialized = request.messages.map((message) => {
    const obj: Record<string, unknown> = { role: message.role };
    if (message.content) {
      obj.content = message.content;
    }
    if (message.tool_calls) obj.tool_calls = message.tool_calls;
    if (message.tool_call_id) {
      obj.tool_call_id = message.tool_call_id;
    }
    return obj;
  });
  return { [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(serialized) ?? "[]" };
}

/**
 * With hideInputs, keep numeric and boolean sampling settings but drop anything that can carry
 * request content (stop strings, tools, response schemas, metadata).
 */
function getInvocationParameters(parameters: Record<string, unknown>, hideInputs?: boolean): string {
  if (!hideInputs) return JSON.stringify(parameters);
  return JSON.stringify(Object.fromEntries(Object.entries(parameters).filter(
    ([, value]) => typeof value === "number" || typeof value === "boolean",
  )));
}

/**
 * Gets tool schema attributes
 */
function getToolsJSONSchema(request: TogetherChatCompletionRequest): Attributes {
  if (!request.tools) {
    return {};
  }
  return { [SemanticConventions.LLM_TOOLS]: safelyJSONStringify(request.tools) ?? "[]" };
}

/**
 * Gets output message attributes from chat response
 */
function getChatOutputMessagesAttributes(response: TogetherChatCompletion): Attributes {
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
function getUsageAttributes(response: TogetherChatCompletion): Attributes {
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
 * Gets usage attributes from completion response
 */
function getCompletionUsageAttributes(response: TogetherCompletion): Attributes {
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
function getEmbeddingOutputAttributes(response: TogetherEmbeddingResponse): Attributes {
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
