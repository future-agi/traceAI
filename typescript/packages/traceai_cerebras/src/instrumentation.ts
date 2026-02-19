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

const MODULE_NAME = "@cerebras/cerebras_cloud_sdk";

/**
 * Flag to check if the Cerebras module has been patched
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

// Type definitions for Cerebras SDK
interface CerebrasMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

interface CerebrasChatCompletionRequest {
  model: string;
  messages: CerebrasMessage[];
  max_tokens?: number;
  temperature?: number;
  top_p?: number;
  stop?: string | string[];
  stream?: boolean;
  [key: string]: unknown;
}

interface CerebrasChatCompletionChoice {
  index: number;
  message: CerebrasMessage;
  finish_reason: string;
  delta?: {
    content?: string;
    role?: string;
  };
}

interface CerebrasChatCompletion {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: CerebrasChatCompletionChoice[];
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
  time_info?: {
    queue_time: number;
    prompt_time: number;
    completion_time: number;
    total_time: number;
  };
}

interface CerebrasStream<T> {
  [Symbol.asyncIterator](): AsyncIterator<T>;
}

/**
 * @param instrumentationConfig The config for the instrumentation
 * @param traceConfig The FI trace configuration for masking sensitive information
 */
export class CerebrasInstrumentation extends InstrumentationBase {
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
      "@traceai/cerebras",
      VERSION,
      Object.assign({}, instrumentationConfig),
    );
    this._traceConfig = traceConfig;
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  public readonly instrumentationName = "@traceai/cerebras";

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "@cerebras/cerebras_cloud_sdk",
      [">=1.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  /**
   * Manually instruments the Cerebras module
   */
  manuallyInstrument(module: Record<string, unknown>) {
    this.patch(module);
  }

  /**
   * Patches the Cerebras module
   */
  private patch(
    module: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    if (module?.fiPatched || _isFIPatched) {
      return module;
    }

    const instrumentation: CerebrasInstrumentation = this;

    // Ensure fiTracer uses the most up-to-date tracer
    if (!(instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer ||
        (instrumentation.fiTracer as unknown as Record<string, unknown>)?.tracer !== instrumentation.tracer) {
      instrumentation.fiTracer = new FITracer({
        tracer: instrumentation.tracer,
        traceConfig: instrumentation._traceConfig
      });
    }

    // Patch the Cerebras class
    const Cerebras = module.Cerebras as {
      prototype: {
        chat?: { completions?: { create?: unknown } };
      };
    } | undefined;

    if (Cerebras?.prototype) {
      // Patch chat.completions.create
      if (Cerebras.prototype.chat?.completions && typeof (Cerebras.prototype.chat.completions as Record<string, unknown>).create === "function") {
        this._wrap(
          Cerebras.prototype.chat.completions as Record<string, unknown>,
          "create",
          this.createChatCompletionWrapper(instrumentation),
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
  private createChatCompletionWrapper(instrumentation: CerebrasInstrumentation) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (original: any): any => {
      return function patchedChatCreate(
        this: unknown,
        body: CerebrasChatCompletionRequest,
        options?: Record<string, unknown>,
      ) {
        const { messages: _messages, ...invocationParameters } = body;

        const span = instrumentation.fiTracer.startSpan(
          `Cerebras Chat Completions`,
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: body.model,
              [SemanticConventions.INPUT_VALUE]: JSON.stringify(body),
              [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                JSON.stringify(invocationParameters),
              [SemanticConventions.LLM_SYSTEM]: LLMSystem.CEREBRAS,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.CEREBRAS,
              ...getChatInputMessagesAttributes(body),
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

        const wrappedPromise = (execPromise as Promise<CerebrasChatCompletion | CerebrasStream<CerebrasChatCompletion>>).then((result) => {
          if (isChatCompletionResponse(result)) {
            span.setAttributes({
              [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
              [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
              [SemanticConventions.LLM_MODEL_NAME]: result.model,
              ...getChatOutputMessagesAttributes(result),
              ...getUsageAttributes(result),
              ...getTimeInfoAttributes(result),
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

        return context.bind(execContext, wrappedPromise) as Promise<CerebrasChatCompletion | CerebrasStream<CerebrasChatCompletion>>;
      };
    };
  }

  /**
   * Un-patches the Cerebras module
   */
  private unpatch(
    moduleExports: Record<string, unknown> & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    const Cerebras = moduleExports.Cerebras as {
      prototype: {
        chat?: { completions?: Record<string, unknown> };
      };
    } | undefined;

    if (Cerebras?.prototype) {
      if (Cerebras.prototype.chat?.completions) {
        this._unwrap(Cerebras.prototype.chat.completions, "create");
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
  response: CerebrasChatCompletion | CerebrasStream<CerebrasChatCompletion>,
): response is CerebrasChatCompletion {
  return "choices" in response && Array.isArray((response as CerebrasChatCompletion).choices);
}

/**
 * Type guard for stream response
 */
function isStreamResponse<T>(
  response: T | CerebrasStream<T>,
): response is CerebrasStream<T> {
  return typeof (response as CerebrasStream<T>)[Symbol.asyncIterator] === "function";
}

/**
 * Wraps a chat stream to capture the full response
 */
async function* wrapChatStream(
  stream: CerebrasStream<CerebrasChatCompletion>,
  span: Span,
): AsyncGenerator<CerebrasChatCompletion, void, unknown> {
  let fullContent = "";
  const allChunks: CerebrasChatCompletion[] = [];
  let lastChunk: CerebrasChatCompletion | null = null;

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
    if (lastChunk.time_info) {
      Object.assign(attributes, getTimeInfoAttributes(lastChunk));
    }
  }

  span.setAttributes(attributes);
  span.setStatus({ code: SpanStatusCode.OK });
  span.end();
}

/**
 * Gets input message attributes for chat requests
 */
function getChatInputMessagesAttributes(request: CerebrasChatCompletionRequest): Attributes {
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
 * Gets output message attributes from chat response
 */
function getChatOutputMessagesAttributes(response: CerebrasChatCompletion): Attributes {
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

  return attributes;
}

/**
 * Gets usage attributes from chat response
 */
function getUsageAttributes(response: CerebrasChatCompletion): Attributes {
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
 * Gets Cerebras-specific time info attributes
 */
function getTimeInfoAttributes(response: CerebrasChatCompletion): Attributes {
  if (response.time_info) {
    return {
      "cerebras.queue_time": response.time_info.queue_time,
      "cerebras.prompt_time": response.time_info.prompt_time,
      "cerebras.completion_time": response.time_info.completion_time,
      "cerebras.total_time": response.time_info.total_time,
    };
  }
  return {};
}
