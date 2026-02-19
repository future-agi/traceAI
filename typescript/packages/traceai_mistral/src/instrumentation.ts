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

const MODULE_NAME = "@mistralai/mistralai";

/**
 * Flag to check if the Mistral module has been patched
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

// Type definitions for Mistral SDK
interface MistralChatMessage {
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

interface MistralChatCompletionChoice {
  index: number;
  message: MistralChatMessage;
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

interface MistralChatCompletion {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: MistralChatCompletionChoice[];
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

interface MistralChatCompletionChunk {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: MistralChatCompletionChoice[];
}

interface MistralChatCompletionCreateParams {
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
  safe_prompt?: boolean;
  random_seed?: number;
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

interface MistralEmbeddingParams {
  model: string;
  inputs: string | string[];
  encoding_format?: string;
}

interface MistralEmbeddingResponse {
  id: string;
  object: string;
  model: string;
  data: Array<{
    object: string;
    embedding: number[];
    index: number;
  }>;
  usage: {
    prompt_tokens: number;
    total_tokens: number;
  };
}

interface MistralStream<T> {
  [Symbol.asyncIterator](): AsyncIterator<T>;
}

/**
 * @param instrumentationConfig The config for the instrumentation
 * @param traceConfig The FI trace configuration for masking sensitive information
 */
export class MistralInstrumentation extends InstrumentationBase {
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;
  public readonly instrumentationName = "@traceai/mistral";

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super(
      "@traceai/mistral",
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
      "@mistralai/mistralai",
      [">=0.1.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  /**
   * Manually instruments the Mistral module
   */
  manuallyInstrument(module: Record<string, unknown>) {
    this.patch(module);
  }

  /**
   * Patches the Mistral module
   */
  private patch(
    module: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    if (module?.fiPatched || _isFIPatched) {
      return module;
    }

    const instrumentation: MistralInstrumentation = this;

    // Ensure fiTracer uses the most up-to-date tracer
    if (!(instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer ||
        (instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer !== instrumentation.tracer) {
      instrumentation.fiTracer = new FITracer({
        tracer: instrumentation.tracer,
        traceConfig: instrumentation._traceConfig
      });
    }

    // Try to patch Mistral client
    // The Mistral SDK uses a class-based approach
    const Mistral = module.Mistral as {
      prototype?: Record<string, unknown>;
    } | undefined;

    if (Mistral?.prototype) {
      // Patch chat method
      if (typeof Mistral.prototype.chat === 'object' && Mistral.prototype.chat !== null) {
        const chatModule = Mistral.prototype.chat as { complete?: unknown; stream?: unknown };

        if (typeof chatModule.complete === 'function') {
          this._wrap(
            chatModule as Record<string, unknown>,
            "complete",
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            (original: any): any => {
              return function patchedComplete(
                this: unknown,
                ...args: [MistralChatCompletionCreateParams]
              ) {
                const body = args[0];
                const { messages: _messages, ...invocationParameters } = body;

                const span = instrumentation.fiTracer.startSpan(
                  `Mistral Chat Completions`,
                  {
                    kind: SpanKind.INTERNAL,
                    attributes: {
                      [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
                      [SemanticConventions.LLM_MODEL_NAME]: body.model,
                      [SemanticConventions.INPUT_VALUE]: JSON.stringify(body),
                      [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
                      [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                        JSON.stringify(invocationParameters),
                      [SemanticConventions.LLM_PROVIDER]: LLMProvider.MISTRALAI,
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

                const wrappedPromise = (execPromise as Promise<MistralChatCompletion>).then((result) => {
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
                  return result;
                });

                return context.bind(execContext, wrappedPromise);
              };
            },
          );
        }
      }

      // Patch embeddings method
      if (typeof Mistral.prototype.embeddings === 'object' && Mistral.prototype.embeddings !== null) {
        const embeddingsModule = Mistral.prototype.embeddings as { create?: unknown };

        if (typeof embeddingsModule.create === 'function') {
          this._wrap(
            embeddingsModule as Record<string, unknown>,
            "create",
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            (original: any): any => {
              return function patchedCreate(
                this: unknown,
                ...args: [MistralEmbeddingParams]
              ) {
                const body = args[0];
                const isStringInput = typeof body.inputs === "string";

                const span = instrumentation.fiTracer.startSpan(`Mistral Embeddings`, {
                  kind: SpanKind.INTERNAL,
                  attributes: {
                    [SemanticConventions.FI_SPAN_KIND]: FISpanKind.EMBEDDING,
                    [SemanticConventions.EMBEDDING_MODEL_NAME]: body.model,
                    [SemanticConventions.INPUT_VALUE]: isStringInput
                      ? body.inputs as string
                      : JSON.stringify(body.inputs),
                    [SemanticConventions.INPUT_MIME_TYPE]: isStringInput
                      ? MimeType.TEXT
                      : MimeType.JSON,
                    [SemanticConventions.LLM_PROVIDER]: LLMProvider.MISTRALAI,
                    [SemanticConventions.GEN_AI_OPERATION_NAME]: "embeddings",
                    ...getEmbeddingTextAttributes(body),
                    [SemanticConventions.RAW_INPUT]: safelyJSONStringify(body) ?? "",
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

                const wrappedPromise = (execPromise as Promise<MistralEmbeddingResponse>).then((result) => {
                  if (result) {
                    span.setAttributes({
                      ...getEmbeddingEmbeddingsAttributes(result),
                      [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
                    });
                  }
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
   * Un-patches the Mistral module
   */
  private unpatch(
    moduleExports: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    const Mistral = moduleExports.Mistral as {
      prototype?: Record<string, unknown>;
    } | undefined;

    if (Mistral?.prototype) {
      const chatModule = Mistral.prototype.chat as Record<string, unknown> | undefined;
      if (chatModule?.complete) {
        this._unwrap(chatModule, "complete");
      }

      const embeddingsModule = Mistral.prototype.embeddings as Record<string, unknown> | undefined;
      if (embeddingsModule?.create) {
        this._unwrap(embeddingsModule, "create");
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
 * Converts the body of a chat completions request to LLM input messages
 */
function getLLMInputMessagesAttributes(
  body: MistralChatCompletionCreateParams,
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
  body: MistralChatCompletionCreateParams,
): Attributes {
  if (!body.tools) {
    return {};
  }
  return { [SemanticConventions.LLM_TOOLS]: safelyJSONStringify(body.tools) ?? "[]" };
}

/**
 * Get usage attributes from completion response
 */
function getUsageAttributes(completion: MistralChatCompletion): Attributes {
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
  chatCompletion: MistralChatCompletion,
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
 * Converts the embedding request payload to embedding attributes
 */
function getEmbeddingTextAttributes(request: MistralEmbeddingParams): Attributes {
  if (typeof request.inputs === "string") {
    return {
      [`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]:
        request.inputs,
    };
  } else if (Array.isArray(request.inputs)) {
    return request.inputs.reduce((acc, input, index) => {
      const indexPrefix = `${SemanticConventions.EMBEDDING_EMBEDDINGS}.${index}.`;
      acc[`${indexPrefix}${SemanticConventions.EMBEDDING_TEXT}`] = input;
      return acc;
    }, {} as Attributes);
  }
  return {};
}

/**
 * Converts the embedding result payload to embedding attributes
 */
function getEmbeddingEmbeddingsAttributes(
  response: MistralEmbeddingResponse,
): Attributes {
  return response.data.reduce((acc, embedding, index) => {
    const indexPrefix = `${SemanticConventions.EMBEDDING_EMBEDDINGS}.${index}.`;
    acc[`${indexPrefix}${SemanticConventions.EMBEDDING_VECTOR}`] =
      embedding.embedding;
    return acc;
  }, {} as Attributes);
}
