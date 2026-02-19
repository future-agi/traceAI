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

const MODULE_NAME = "together-ai";

/**
 * Flag to check if the together module has been patched
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
}

/**
 * @param instrumentationConfig The config for the instrumentation
 * @param traceConfig The FI trace configuration for masking sensitive information
 */
export class TogetherInstrumentation extends InstrumentationBase {
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
      "@traceai/together",
      VERSION,
      Object.assign({}, instrumentationConfig),
    );
    this._traceConfig = traceConfig;
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  public readonly instrumentationName = "@traceai/together";

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "together-ai",
      [">=0.5.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  /**
   * Manually instruments the Together module
   */
  manuallyInstrument(module: Record<string, unknown>) {
    this.patch(module);
  }

  /**
   * Patches the Together module
   */
  private patch(
    module: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    if (module?.fiPatched || _isFIPatched) {
      return module;
    }

    const instrumentation: TogetherInstrumentation = this;

    // Ensure fiTracer uses the most up-to-date tracer
    if (!(instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer ||
        (instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer !== instrumentation.tracer) {
      instrumentation.fiTracer = new FITracer({
        tracer: instrumentation.tracer,
        traceConfig: instrumentation._traceConfig
      });
    }

    // Patch the Together class
    const Together = module.Together as {
      prototype: {
        chat?: { completions?: { create?: unknown } };
        completions?: { create?: unknown };
        embeddings?: { create?: unknown };
      };
    } | undefined;

    if (Together?.prototype) {
      // Patch chat.completions.create
      if (Together.prototype.chat?.completions && typeof (Together.prototype.chat.completions as Record<string, unknown>).create === "function") {
        this._wrap(
          Together.prototype.chat.completions as Record<string, unknown>,
          "create",
          this.createChatCompletionWrapper(instrumentation),
        );
      }

      // Patch completions.create (legacy)
      if (Together.prototype.completions && typeof (Together.prototype.completions as Record<string, unknown>).create === "function") {
        this._wrap(
          Together.prototype.completions as Record<string, unknown>,
          "create",
          this.createCompletionWrapper(instrumentation),
        );
      }

      // Patch embeddings.create
      if (Together.prototype.embeddings && typeof (Together.prototype.embeddings as Record<string, unknown>).create === "function") {
        this._wrap(
          Together.prototype.embeddings as Record<string, unknown>,
          "create",
          this.createEmbeddingWrapper(instrumentation),
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
                JSON.stringify(invocationParameters),
              [SemanticConventions.LLM_SYSTEM]: LLMSystem.TOGETHER,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.TOGETHER,
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

        const wrappedPromise = (execPromise as Promise<TogetherChatCompletion | TogetherStream<TogetherChatCompletion>>).then((result) => {
          if (isChatCompletionResponse(result)) {
            span.setAttributes({
              [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
              [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_MODEL_NAME]: result.model,
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
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
        });

        return context.bind(execContext, wrappedPromise) as Promise<TogetherChatCompletion | TogetherStream<TogetherChatCompletion>>;
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
                JSON.stringify(invocationParameters),
              [SemanticConventions.LLM_SYSTEM]: LLMSystem.TOGETHER,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.TOGETHER,
              [`${SemanticConventions.LLM_PROMPTS}.0`]: body.prompt,
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

        const wrappedPromise = (execPromise as Promise<TogetherCompletion>).then((result) => {
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
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
        });

        return context.bind(execContext, wrappedPromise) as Promise<TogetherCompletion>;
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
              [SemanticConventions.LLM_SYSTEM]: LLMSystem.TOGETHER,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.TOGETHER,
              ...getEmbeddingInputAttributes(inputTexts),
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

        const wrappedPromise = (execPromise as Promise<TogetherEmbeddingResponse>).then((result) => {
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
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
        });

        return context.bind(execContext, wrappedPromise) as Promise<TogetherEmbeddingResponse>;
      };
    };
  }

  /**
   * Un-patches the Together module
   */
  private unpatch(
    moduleExports: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    const Together = moduleExports.Together as {
      prototype: {
        chat?: { completions?: Record<string, unknown> };
        completions?: Record<string, unknown>;
        embeddings?: Record<string, unknown>;
      };
    } | undefined;

    if (Together?.prototype) {
      if (Together.prototype.chat?.completions) {
        this._unwrap(Together.prototype.chat.completions, "create");
      }
      if (Together.prototype.completions) {
        this._unwrap(Together.prototype.completions, "create");
      }
      if (Together.prototype.embeddings) {
        this._unwrap(Together.prototype.embeddings, "create");
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
 * Wraps a chat stream to capture the full response
 */
async function* wrapChatStream(
  stream: TogetherStream<TogetherChatCompletion>,
  span: Span,
): AsyncGenerator<TogetherChatCompletion, void, unknown> {
  let fullContent = "";
  const allChunks: TogetherChatCompletion[] = [];
  let lastChunk: TogetherChatCompletion | null = null;

  try {
    for await (const chunk of stream) {
      allChunks.push(chunk);
      const choice = chunk.choices?.[0];
      if (choice?.delta?.content) {
        fullContent += choice.delta.content;
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

  const messageIndexPrefix = `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.`;
  const attributes: Attributes = {
    [SemanticConventions.OUTPUT_VALUE]: fullContent,
    [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
    [`${messageIndexPrefix}${SemanticConventions.MESSAGE_CONTENT}`]: fullContent,
    [`${messageIndexPrefix}${SemanticConventions.MESSAGE_ROLE}`]: "assistant",
    [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(allChunks) ?? "",
  };

  if (lastChunk) {
    attributes[SemanticConventions.LLM_MODEL_NAME] = lastChunk.model;
    if (lastChunk.usage) {
      attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT] = lastChunk.usage.prompt_tokens;
      attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION] = lastChunk.usage.completion_tokens;
      attributes[SemanticConventions.LLM_TOKEN_COUNT_TOTAL] = lastChunk.usage.total_tokens;
    }
  }

  span.setAttributes(attributes);
  span.setStatus({ code: SpanStatusCode.OK });
  span.end();
}

/**
 * Gets input message attributes for chat requests
 */
function getChatInputMessagesAttributes(request: TogetherChatCompletionRequest): Attributes {
  return request.messages.reduce((acc, message, index) => {
    const indexPrefix = `${SemanticConventions.LLM_INPUT_MESSAGES}.${index}.`;
    acc[`${indexPrefix}${SemanticConventions.MESSAGE_ROLE}`] = message.role;
    if (message.content) {
      acc[`${indexPrefix}${SemanticConventions.MESSAGE_CONTENT}`] = message.content;
    }
    if (message.tool_call_id) {
      acc[`${indexPrefix}${SemanticConventions.MESSAGE_TOOL_CALL_ID}`] = message.tool_call_id;
    }
    return acc;
  }, {} as Attributes);
}

/**
 * Gets tool schema attributes
 */
function getToolsJSONSchema(request: TogetherChatCompletionRequest): Attributes {
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
function getChatOutputMessagesAttributes(response: TogetherChatCompletion): Attributes {
  const choice = response.choices[0];
  if (!choice) {
    return {};
  }

  const indexPrefix = `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.`;
  const attributes: Attributes = {
    [`${indexPrefix}${SemanticConventions.MESSAGE_ROLE}`]: choice.message.role,
  };

  if (choice.message.content) {
    attributes[`${indexPrefix}${SemanticConventions.MESSAGE_CONTENT}`] = choice.message.content;
  }

  if (choice.message.tool_calls) {
    choice.message.tool_calls.forEach((toolCall, index) => {
      const toolCallIndexPrefix = `${indexPrefix}${SemanticConventions.MESSAGE_TOOL_CALLS}.${index}.`;
      if (toolCall.id) {
        attributes[`${toolCallIndexPrefix}${SemanticConventions.TOOL_CALL_ID}`] = toolCall.id;
      }
      if (toolCall.function) {
        attributes[`${toolCallIndexPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_NAME}`] =
          toolCall.function.name;
        attributes[`${toolCallIndexPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}`] =
          toolCall.function.arguments;
      }
    });
  }

  return attributes;
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
