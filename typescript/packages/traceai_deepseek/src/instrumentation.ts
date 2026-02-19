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

const MODULE_NAME = "openai";

/**
 * Flag to check if the DeepSeek module has been patched
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

// Type definitions for OpenAI-compatible API (DeepSeek)
interface ChatMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  reasoning_content?: string; // DeepSeek R1 reasoning
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
  finish_reason: string;
  delta?: {
    content?: string;
    reasoning_content?: string;
    role?: string;
    tool_calls?: ToolCall[];
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
    prompt_cache_hit_tokens?: number;
    prompt_cache_miss_tokens?: number;
  };
}

interface StreamResponse<T> {
  [Symbol.asyncIterator](): AsyncIterator<T>;
}

/**
 * @param instrumentationConfig The config for the instrumentation
 * @param traceConfig The FI trace configuration for masking sensitive information
 */
export class DeepSeekInstrumentation extends InstrumentationBase {
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
      "@traceai/deepseek",
      VERSION,
      Object.assign({}, instrumentationConfig),
    );
    this._traceConfig = traceConfig;
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  public readonly instrumentationName = "@traceai/deepseek";

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
   * Manually instruments the OpenAI module for DeepSeek
   */
  manuallyInstrument(module: Record<string, unknown>) {
    this.patch(module);
  }

  /**
   * Checks if a request is targeting DeepSeek API
   */
  private isDeepSeekRequest(client: unknown): boolean {
    const baseURL = (client as { baseURL?: string })?.baseURL;
    if (!baseURL) return false;
    return baseURL.includes("deepseek") || baseURL.includes("api.deepseek.com");
  }

  /**
   * Patches the OpenAI module for DeepSeek
   */
  private patch(
    module: Record<string, unknown> & { fiDeepSeekPatched?: boolean },
    moduleVersion?: string,
  ) {
    if (module?.fiDeepSeekPatched || _isFIPatched) {
      return module;
    }

    const instrumentation: DeepSeekInstrumentation = this;

    // Ensure fiTracer uses the most up-to-date tracer
    if (!(instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer ||
        (instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer !== instrumentation.tracer) {
      instrumentation.fiTracer = new FITracer({
        tracer: instrumentation.tracer,
        traceConfig: instrumentation._traceConfig
      });
    }

    // Patch the OpenAI class
    const OpenAI = module.OpenAI as {
      prototype: {
        chat?: { completions?: { create?: unknown } };
      };
    } | undefined;

    if (OpenAI?.prototype) {
      // Patch chat.completions.create
      if (OpenAI.prototype.chat?.completions && typeof (OpenAI.prototype.chat.completions as Record<string, unknown>).create === "function") {
        this._wrap(
          OpenAI.prototype.chat.completions as Record<string, unknown>,
          "create",
          this.createChatCompletionWrapper(instrumentation),
        );
      }
    }

    _isFIPatched = true;
    try {
      module.fiDeepSeekPatched = true;
    } catch (e) {
      diag.warn(`Failed to set fiDeepSeekPatched flag on module '${MODULE_NAME}'. Error: ${e}`);
    }

    return module;
  }

  /**
   * Creates a wrapper for chat.completions.create
   */
  private createChatCompletionWrapper(instrumentation: DeepSeekInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedChatCreate(
        this: unknown,
        body: ChatCompletionRequest,
        options?: Record<string, unknown>,
      ) {
        if (!instrumentation.isDeepSeekRequest(this)) {
          return original.apply(this, [body, options]);
        }

        const { messages: _messages, ...invocationParameters } = body;

        const span = instrumentation.fiTracer.startSpan(
          `DeepSeek Chat Completions`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: body.model,
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(body),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                JSON.stringify(invocationParameters),
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.DEEPSEEK,
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

        const wrappedPromise = (execPromise as Promise<ChatCompletion | StreamResponse<ChatCompletion>>).then((result) => {
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
        }).catch((error: Error) => {
          span.recordException(error);
          span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
          span.end();
          throw error;
        });

        return context.bind(execContext, wrappedPromise) as Promise<ChatCompletion | StreamResponse<ChatCompletion>>;
      };
    };
  }

  /**
   * Un-patches the OpenAI module
   */
  private unpatch(
    moduleExports: Record<string, unknown> & { fiDeepSeekPatched?: boolean },
    moduleVersion?: string,
  ) {
    const OpenAI = moduleExports.OpenAI as {
      prototype: {
        chat?: { completions?: Record<string, unknown> };
      };
    } | undefined;

    if (OpenAI?.prototype) {
      if (OpenAI.prototype.chat?.completions) {
        this._unwrap(OpenAI.prototype.chat.completions, "create");
      }
    }

    _isFIPatched = false;
    try {
      moduleExports.fiDeepSeekPatched = false;
    } catch (e) {
      diag.warn(`Failed to unset fiDeepSeekPatched flag on module '${MODULE_NAME}'. Error: ${e}`);
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

/**
 * Wraps a chat stream to capture the full response
 */
async function* wrapChatStream(
  stream: StreamResponse<ChatCompletion>,
  span: Span,
): AsyncGenerator<ChatCompletion, void, unknown> {
  let fullContent = "";
  let fullReasoningContent = "";
  const allChunks: ChatCompletion[] = [];
  let lastChunk: ChatCompletion | null = null;

  try {
    for await (const chunk of stream) {
      allChunks.push(chunk);
      const choice = chunk.choices?.[0];
      if (choice?.delta?.content) {
        fullContent += choice.delta.content;
      }
      if (choice?.delta?.reasoning_content) {
        fullReasoningContent += choice.delta.reasoning_content;
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

  const msg: Record<string, unknown> = { role: "assistant", content: fullContent };
  const attributes: Attributes = {
    [SemanticConventions.OUTPUT_VALUE]: fullContent,
    [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
    [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([msg]) ?? "[]",
    [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(allChunks) ?? "",
  };

  // Capture reasoning content for DeepSeek R1 models
  if (fullReasoningContent) {
    attributes["deepseek.reasoning_content"] = fullReasoningContent;
  }

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

  const attributes: Attributes = {
    [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([msg]) ?? "[]",
  };

  // Capture reasoning content for DeepSeek R1 models
  if (choice.message.reasoning_content) {
    attributes["deepseek.reasoning_content"] = choice.message.reasoning_content;
  }

  return attributes;
}

/**
 * Gets usage attributes from chat response
 */
function getUsageAttributes(response: ChatCompletion): Attributes {
  if (response.usage) {
    const attrs: Attributes = {
      [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]: response.usage.completion_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_PROMPT]: response.usage.prompt_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_TOTAL]: response.usage.total_tokens,
    };
    // DeepSeek-specific cache metrics
    if (response.usage.prompt_cache_hit_tokens !== undefined) {
      attrs["deepseek.prompt_cache_hit_tokens"] = response.usage.prompt_cache_hit_tokens;
    }
    if (response.usage.prompt_cache_miss_tokens !== undefined) {
      attrs["deepseek.prompt_cache_miss_tokens"] = response.usage.prompt_cache_miss_tokens;
    }
    return attrs;
  }
  return {};
}
