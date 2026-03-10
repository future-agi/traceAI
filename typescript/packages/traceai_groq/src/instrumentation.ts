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

const MODULE_NAME = "groq-sdk";

/**
 * Flag to check if the groq module has been patched
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

// Type definitions for Groq SDK
interface GroqChatCompletionMessage {
  role: string;
  content: string | null;
  tool_calls?: Array<{
    id: string;
    type: string;
    function: {
      name: string;
      arguments: string;
    };
  }>;
}

interface GroqChatCompletionChoice {
  index: number;
  message: GroqChatCompletionMessage;
  finish_reason: string;
  delta?: {
    content?: string;
    tool_calls?: Array<{
      id?: string;
      function?: {
        name?: string;
        arguments?: string;
      };
    }>;
  };
}

interface GroqChatCompletion {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: GroqChatCompletionChoice[];
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

interface GroqChatCompletionChunk {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: GroqChatCompletionChoice[];
}

interface GroqChatCompletionCreateParams {
  model: string;
  messages: Array<{
    role: string;
    content: string;
    tool_call_id?: string;
    name?: string;
  }>;
  temperature?: number;
  max_tokens?: number;
  top_p?: number;
  stream?: boolean;
  tools?: Array<{
    type: string;
    function: {
      name: string;
      description?: string;
      parameters?: Record<string, unknown>;
    };
  }>;
  tool_choice?: string | { type: string; function: { name: string } };
  [key: string]: unknown;
}

interface GroqStream<T> {
  [Symbol.asyncIterator](): AsyncIterator<T>;
  tee(): [GroqStream<T>, GroqStream<T>];
}

/**
 * @param instrumentationConfig The config for the instrumentation
 * @param traceConfig The FI trace configuration for masking sensitive information
 */
export class GroqInstrumentation extends InstrumentationBase {
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
      "@traceai/groq",
      VERSION,
      Object.assign({}, instrumentationConfig),
    );
    this._traceConfig = traceConfig;
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  public readonly instrumentationName = "@traceai/groq";

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "groq-sdk",
      [">=0.3.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  /**
   * Manually instruments the Groq module
   */
  manuallyInstrument(module: Record<string, unknown>) {
    this.patch(module);
  }

  /**
   * Patches the Groq module
   */
  private patch(
    module: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    if (module?.fiPatched || _isFIPatched) {
      return module;
    }

    const instrumentation: GroqInstrumentation = this;

    // Ensure fiTracer uses the most up-to-date tracer
    if (!(instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer ||
        (instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer !== instrumentation.tracer) {
      instrumentation.fiTracer = new FITracer({
        tracer: instrumentation.tracer,
        traceConfig: instrumentation._traceConfig
      });
    }

    // Patch Chat Completions
    const Groq = module.Groq as {
      Chat?: { Completions?: { prototype: Record<string, unknown> } };
    } | undefined;

    if (Groq?.Chat?.Completions?.prototype) {
      type ChatCompletionCreateType = (
        body: GroqChatCompletionCreateParams,
        options?: Record<string, unknown>
      ) => Promise<GroqChatCompletion | GroqStream<GroqChatCompletionChunk>>;

      this._wrap(
        Groq.Chat.Completions.prototype,
        "create",
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (original: any): any => {
          return function patchedCreate(
            this: unknown,
            ...args: [GroqChatCompletionCreateParams, Record<string, unknown>?]
          ) {
            const body = args[0];
            const { messages: _messages, ...invocationParameters } = body;

            const span = instrumentation.fiTracer.startSpan(
              `Groq Chat Completions`,
              {
                kind: SpanKind.INTERNAL,
                attributes: {
                  [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
                  [SemanticConventions.LLM_MODEL_NAME]: body.model,
                  [SemanticConventions.INPUT_VALUE]: JSON.stringify(body),
                  [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
                  [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                    JSON.stringify(invocationParameters),
                  [SemanticConventions.LLM_PROVIDER]: LLMProvider.GROQ,
                  [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
                  ...getLLMInputMessagesAttributes(body),
                  ...getLLMToolsJSONSchema(body),
                  [SemanticConventions.RAW_INPUT]: safelyJSONStringify(body) ?? "",
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

            const wrappedPromise = (execPromise as Promise<GroqChatCompletion | GroqStream<GroqChatCompletionChunk>>).then((result) => {
              if (isChatCompletionResponse(result)) {
                span.setAttributes({
                  [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
                  [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
                  [SemanticConventions.LLM_MODEL_NAME]: result.model,
                  [SemanticConventions.GEN_AI_RESPONSE_MODEL]: result.model,
                  [SemanticConventions.GEN_AI_RESPONSE_ID]: result.id,
                  [SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS]: safelyJSONStringify(result.choices.map(c => c.finish_reason)) ?? "[]",
                  ...getChatCompletionLLMOutputMessagesAttributes(result),
                  ...getUsageAttributes(result),
                  [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
                });
                span.setStatus({ code: SpanStatusCode.OK });
                span.end();
              } else if (isStreamResponse(result)) {
                const [leftStream, rightStream] = result.tee();
                consumeChatCompletionStreamChunks(rightStream, span);
                result = leftStream as unknown as typeof result;
              }
              return result;
            }).catch((error: Error) => {
              span.recordException(error);
              span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
              span.end();
              throw error;
            });

            return context.bind(execContext, wrappedPromise) as Promise<GroqChatCompletion | GroqStream<GroqChatCompletionChunk>>;
          };
        },
      );
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
   * Un-patches the Groq module
   */
  private unpatch(
    moduleExports: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    const Groq = moduleExports.Groq as {
      Chat?: { Completions?: { prototype: Record<string, unknown> } };
    } | undefined;

    if (Groq?.Chat?.Completions?.prototype) {
      this._unwrap(Groq.Chat.Completions.prototype, "create");
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
  response: GroqChatCompletion | GroqStream<GroqChatCompletionChunk>,
): response is GroqChatCompletion {
  return "choices" in response && Array.isArray((response as GroqChatCompletion).choices);
}

/**
 * Type guard for stream response
 */
function isStreamResponse(
  response: GroqChatCompletion | GroqStream<GroqChatCompletionChunk>,
): response is GroqStream<GroqChatCompletionChunk> {
  return typeof (response as GroqStream<GroqChatCompletionChunk>)[Symbol.asyncIterator] === "function";
}

/**
 * Converts the body of a chat completions request to LLM input messages
 */
function getLLMInputMessagesAttributes(
  body: GroqChatCompletionCreateParams,
): Attributes {
  const serialized = body.messages.map((message) => {
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
 * Converts each tool definition into a json schema
 */
function getLLMToolsJSONSchema(
  body: GroqChatCompletionCreateParams,
): Attributes {
  if (!body.tools) {
    return {};
  }
  return { [SemanticConventions.LLM_TOOLS]: safelyJSONStringify(body.tools) ?? "[]" };
}

/**
 * Get usage attributes from completion response
 */
function getUsageAttributes(completion: GroqChatCompletion): Attributes {
  if (completion.usage) {
    return {
      [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]:
        completion.usage.completion_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_PROMPT]:
        completion.usage.prompt_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_TOTAL]:
        completion.usage.total_tokens,
    };
  }
  return {};
}

/**
 * Converts the chat completion result to LLM output attributes
 */
function getChatCompletionLLMOutputMessagesAttributes(
  chatCompletion: GroqChatCompletion,
): Attributes {
  const choice = chatCompletion.choices[0];
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
 * Consumes the stream chunks and adds them to the span
 */
async function consumeChatCompletionStreamChunks(
  stream: GroqStream<GroqChatCompletionChunk>,
  span: Span,
) {
  let streamResponse = "";
  const toolCalls: Record<number, { id?: string; name?: string; arguments?: string }> = {};
  const allChunks: GroqChatCompletionChunk[] = [];

  try {
    for await (const chunk of stream) {
      allChunks.push(chunk);
      if (chunk.choices.length <= 0) {
        continue;
      }
      const choice = chunk.choices[0];
      if (choice.delta?.content) {
        streamResponse += choice.delta.content;
      }

      if (choice.delta?.tool_calls) {
        choice.delta.tool_calls.forEach((toolCall, index) => {
          if (!toolCalls[index]) {
            toolCalls[index] = {};
          }
          if (toolCall.id) {
            toolCalls[index].id = toolCall.id;
          }
          if (toolCall.function?.name) {
            toolCalls[index].name = (toolCalls[index].name || "") + toolCall.function.name;
          }
          if (toolCall.function?.arguments) {
            toolCalls[index].arguments = (toolCalls[index].arguments || "") + toolCall.function.arguments;
          }
        });
      }
    }
  } catch (error) {
    span.recordException(error as Error);
    span.setStatus({ code: SpanStatusCode.ERROR, message: (error as Error).message });
    span.end();
    return;
  }

  const msg: Record<string, unknown> = { role: "assistant", content: streamResponse };
  const toolCallEntries = Object.values(toolCalls);
  if (toolCallEntries.length > 0) {
    msg.tool_calls = toolCallEntries.map((tc) => ({
      id: tc.id,
      function: { name: tc.name, arguments: tc.arguments },
    }));
  }

  const attributes: Attributes = {
    [SemanticConventions.OUTPUT_VALUE]: streamResponse,
    [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
    [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([msg]) ?? "[]",
    [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(allChunks) ?? "",
  };

  span.setAttributes(attributes);
  span.setStatus({ code: SpanStatusCode.OK });
  span.end();
}
