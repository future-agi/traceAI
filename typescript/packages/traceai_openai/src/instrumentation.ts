import openai from "openai";
import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationModuleDefinition,
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
import {
  ChatCompletion,
  ChatCompletionChunk,
  ChatCompletionCreateParamsBase,
  ChatCompletionMessage,
  ChatCompletionMessageParam,
} from "openai/resources/chat/completions";
import { CompletionCreateParamsBase } from "openai/resources/completions";
import { Stream } from "openai/streaming";
import {
  Completion,
  CreateEmbeddingResponse,
  EmbeddingCreateParams,
} from "openai/resources";
import { assertUnreachable, isString } from "./typeUtils";
import { isTracingSuppressed } from "@opentelemetry/core";

import {
  FITracer,
  safelyJSONStringify,
  TraceConfigOptions,
} from "@traceai/fi-core";
import {
  ResponseCreateParamsBase,
  ResponseStreamEvent,
  Response as ResponseType,
} from "openai/resources/responses/responses";

import {
  consumeResponseStreamEvents,
  getResponsesInputMessagesAttributes,
  getResponsesOutputMessagesAttributes,
  getResponsesUsageAttributes,
} from "./responseAttributes"; 

const MODULE_NAME = "openai";

/**
 * Flag to check if the openai module has been patched
 * Note: This is a fallback in case the module is made immutable (e.x. Deno, webpack, etc.)
 */
let _isFIPatched = false;

/**
 * function to check if instrumentation is enabled / disabled
 */
export function isPatched() {
  return _isFIPatched;
}

/**
 * Resolves the execution context for the current span
 * If tracing is suppressed, the span is dropped and the current context is returned
 * @param span
 */
function getExecContext(span: Span) {
  const activeContext = context.active();
  const suppressTracing = isTracingSuppressed(activeContext);
  const execContext = suppressTracing
    ? trace.setSpan(context.active(), span)
    : activeContext;
  // Drop the span from the context
  if (suppressTracing) {
    trace.deleteSpan(activeContext);
  }
  return execContext;
}
/**
 * @param instrumentationConfig The config for the instrumentation @see {@link InstrumentationConfig}
 * @param traceConfig The FI trace configuration. Can be used to mask or redact sensitive information on spans. @see {@link TraceConfigOptions}
 */
export class OpenAIInstrumentation extends InstrumentationBase{
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    /**
     * The config for the instrumentation
     * @see {@link InstrumentationConfig}
     */
    instrumentationConfig?: InstrumentationConfig;
    /**
     * The fi trace configuration. Can be used to mask or redact sensitive information on spans.
     * @see {@link TraceConfigOptions}
     */
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super(
      "@traceai/fi-instrumentation-openai",
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
      "openai",
      ["^4.0.0", "^5.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  /**
   * Manually instruments the OpenAI module. This is needed when the module is not loaded via require (commonjs)
   * @param {openai} module
   */
  manuallyInstrument(module: typeof openai) {
    // diag.debug(`Manually instrumenting ${MODULE_NAME}`);
    this.patch(module);
  }

  /**
   * Patches the OpenAI module
   */
  private patch(
    module: typeof openai & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    // diag.debug(`Applying patch for ${MODULE_NAME}@${moduleVersion}`);
    if (module?.fiPatched || _isFIPatched) {
      return module;
    }
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    const instrumentation: OpenAIInstrumentation = this;

    // ADDED: Ensure fiTracer uses the most up-to-date tracer from InstrumentationBase
    // The `tracer` property in `InstrumentationBase` (accessed via `instrumentation.tracer`)
    // is initialized as a ProxyTracer and then updated to the actual Tracer.
    // FITracer might have been initialized with the ProxyTracer in `enable()`.
    // Here, we ensure it uses the (potentially updated) Tracer.
    // Accessing FITracer's internal tracer `tracer` for comparison.
    // This assumes FITracer has an internal property named `tracer`.
    if (!(instrumentation.fiTracer as any).tracer || (instrumentation.fiTracer as any).tracer !== instrumentation.tracer) {
        // diag.debug(
        //     `OpenAIInstrumentation.patch: fiTracer's internal tracer (${(instrumentation.fiTracer as any).tracer?.constructor?.name}) ` +
        //     `differs from current base tracer (${instrumentation.tracer?.constructor?.name}) or is not set. Re-initializing fiTracer.`
        // );
        instrumentation.fiTracer = new FITracer({ tracer: instrumentation.tracer, traceConfig: instrumentation._traceConfig });
    } else {
        // diag.debug(
        //     `OpenAIInstrumentation.patch: fiTracer already using current base tracer (${instrumentation.tracer?.constructor?.name}). No re-initialization needed.`
        // );
    }

    // Patch create chat completions
    type ChatCompletionCreateType =
      typeof module.OpenAI.Chat.Completions.prototype.create;

    this._wrap(
      module.OpenAI.Chat.Completions.prototype,
      "create",
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (original: ChatCompletionCreateType): any => {
        return function patchedCreate(
          this: unknown,
          ...args: Parameters<ChatCompletionCreateType>
        ) {
          const body = args[0];
          const { messages: _messages, ...invocationParameters } = body;

          // diag.debug("@traceai/openai: ChatCompletion patch CALLED. Starting span...");
          
          // --- ADD LOGS FOR TRACER AND CONTEXT ---
          // const activeContextForSuppressionCheck = context.active();
          // const isSuppressed = isTracingSuppressed(activeContextForSuppressionCheck);
          // diag.debug(`@traceai/openai: Is tracing suppressed? ${isSuppressed}`);
          // diag.debug(`@traceai/openai: this.tracer type: ${instrumentation.tracer?.constructor?.name}`); // Accessing the base tracer
          // diag.debug(`@traceai/openai: this.fiTracer type: ${instrumentation.fiTracer?.constructor?.name}`);
          // --- END ADDED LOGS ---

          const span = instrumentation.fiTracer.startSpan(
            `OpenAI Chat Completions`,
            {
              kind: SpanKind.INTERNAL,
              attributes: {
                [SemanticConventions.FI_SPAN_KIND]:
                  FISpanKind.LLM,
                [SemanticConventions.LLM_MODEL_NAME]: body.model,
                [SemanticConventions.INPUT_VALUE]: JSON.stringify(body),
                [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
                [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                  JSON.stringify(invocationParameters),
                [SemanticConventions.LLM_PROVIDER]: LLMProvider.OPENAI,
                [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
                ...getLLMInputMessagesAttributes(body),
                ...getLLMToolsJSONSchema(body),
                [SemanticConventions.RAW_INPUT]: safelyJSONStringify(body) ?? "",
              },
            },
          );

          // --- ADD LOG ---
          // diag.debug(`@traceai/openai: ChatCompletion span STARTED: ${span.spanContext().spanId}`);

          const execContext = getExecContext(span);
          const execPromise = safeExecuteInTheMiddle<
            ReturnType<ChatCompletionCreateType>
          >(
            () => {
              return context.with(trace.setSpan(execContext, span), () => {
                return original.apply(this, args);
              });
            },
            (error) => {
              if (error) {
                // --- ADD LOG ---
                diag.error(`@traceai/openai: ChatCompletion error in safeExecuteInTheMiddle: ${error}`);
                span.recordException(error);
                span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
                span.end();
                // --- ADD LOG ---
                // diag.debug(`@traceai/openai: ChatCompletion span ENDED due to error: ${span.spanContext().spanId}`);
              }
            },
          );

          const wrappedPromise = execPromise.then((result) => {
            if (isChatCompletionResponse(result)) {
              span.setAttributes({
                [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
                [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
                // Override the model from the value sent by the server
                [SemanticConventions.LLM_MODEL_NAME]: result.model,
                [SemanticConventions.GEN_AI_RESPONSE_MODEL]: result.model,
                [SemanticConventions.GEN_AI_RESPONSE_ID]: result.id,
                [SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS]: JSON.stringify(
                  result.choices.map((c) => c.finish_reason).filter(Boolean),
                ),
                ...getChatCompletionLLMOutputMessagesAttributes(result),
                ...getUsageAttributes(result),
                [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
              });
              span.setStatus({ code: SpanStatusCode.OK });
              span.end();
            } else {
              const [leftStream, rightStream] = result.tee();
              consumeChatCompletionStreamChunks(rightStream, span);
              result = leftStream;
            }
            return result;
          }).catch((error: Error) => {
            span.recordException(error);
            span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
            span.end();
            throw error;
          });
          return context.bind(execContext, wrappedPromise);
        };
      },
    );

    // Patch create completions
    type CompletionsCreateType =
      typeof module.OpenAI.Completions.prototype.create;

    this._wrap(
      module.OpenAI.Completions.prototype,
      "create",
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (original: CompletionsCreateType): any => {
        return function patchedCreate(
          this: unknown,
          ...args: Parameters<CompletionsCreateType>
        ) {
          const body = args[0];
          const { prompt: _prompt, ...invocationParameters } = body;
          const span = instrumentation.fiTracer.startSpan(
            `OpenAI Completions`,
            {
              kind: SpanKind.INTERNAL,
              attributes: {
                [SemanticConventions.FI_SPAN_KIND]:
                  FISpanKind.LLM,
                [SemanticConventions.LLM_MODEL_NAME]: body.model,
                [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                  JSON.stringify(invocationParameters),
                [SemanticConventions.LLM_PROVIDER]: LLMProvider.OPENAI,
                [SemanticConventions.GEN_AI_OPERATION_NAME]: "text_completion",
                ...getCompletionInputValueAndMimeType(body),
                [SemanticConventions.RAW_INPUT]: safelyJSONStringify(body) ?? "",
              },
            },
          );
          const execContext = getExecContext(span);

          const execPromise = safeExecuteInTheMiddle<
            ReturnType<CompletionsCreateType>
          >(
            () => {
              return context.with(trace.setSpan(execContext, span), () => {
                return original.apply(this, args);
              });
            },
            (error) => {
              // Push the error to the span
              if (error) {
                span.recordException(error);
                span.setStatus({
                  code: SpanStatusCode.ERROR,
                  message: error.message,
                });
                span.end();
              }
            },
          );
          const wrappedPromise = execPromise.then((result) => {
            if (isCompletionResponse(result)) {
              span.setAttributes({
                [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
                [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
                [SemanticConventions.LLM_MODEL_NAME]: result.model,
                [SemanticConventions.GEN_AI_RESPONSE_MODEL]: result.model,
                [SemanticConventions.GEN_AI_RESPONSE_ID]: result.id,
                [SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS]: JSON.stringify(
                  result.choices.map((c) => c.finish_reason).filter(Boolean),
                ),
                ...getCompletionOutputValueAndMimeType(result),
                ...getUsageAttributes(result),
                [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
              });
              span.setStatus({ code: SpanStatusCode.OK });
              span.end();
            }
            return result;
          }).catch((error: Error) => {
            span.recordException(error);
            span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
            span.end();
            throw error;
          });
          return context.bind(execContext, wrappedPromise);
        };
      },
    );

    // Patch embeddings
    type EmbeddingsCreateType =
      typeof module.OpenAI.Embeddings.prototype.create;
    this._wrap(
      module.OpenAI.Embeddings.prototype,
      "create",
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (original: EmbeddingsCreateType): any => {
        return function patchedEmbeddingCreate(
          this: unknown,
          ...args: Parameters<EmbeddingsCreateType>
        ) {
          const body = args[0];
          const { input } = body;
          const isStringInput = typeof input === "string";
          const span = instrumentation.fiTracer.startSpan(`OpenAI Embeddings`, {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]:
                FISpanKind.EMBEDDING,
              [SemanticConventions.EMBEDDING_MODEL_NAME]: body.model,
              [SemanticConventions.INPUT_VALUE]: isStringInput
                ? input
                : JSON.stringify(input),
              [SemanticConventions.INPUT_MIME_TYPE]: isStringInput
                ? MimeType.TEXT
                : MimeType.JSON,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "embeddings",
              ...getEmbeddingTextAttributes(body),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(body) ?? "",
            },
          });
          const execContext = getExecContext(span);
          const execPromise = safeExecuteInTheMiddle<
            ReturnType<EmbeddingsCreateType>
          >(
            () => {
              return context.with(trace.setSpan(execContext, span), () => {
                return original.apply(this, args);
              });
            },
            (error) => {
              // Push the error to the span
              if (error) {
                span.recordException(error);
                span.setStatus({
                  code: SpanStatusCode.ERROR,
                  message: error.message,
                });
                span.end();
              }
            },
          );
          const wrappedPromise = execPromise.then((result) => {
            if (result) {
              // Record the results
              span.setAttributes({
                // Do not record the output data as it can be large
                ...getEmbeddingEmbeddingsAttributes(result),
                [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
              });
            }
            span.setStatus({ code: SpanStatusCode.OK });
            span.end();
            return result;
          }).catch((error: Error) => {
            span.recordException(error);
            span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
            span.end();
            throw error;
          });
          return context.bind(execContext, wrappedPromise);
        };
      },
    );

    // Patch responses (if the patched module contains the Responses interface)
    if (module.OpenAI.Responses) {
      type ResponsesCreateType =
        typeof module.OpenAI.Responses.prototype.create;

      this._wrap(
        module.OpenAI.Responses.prototype,
        "create",
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (original: ResponsesCreateType): any => {
          return function patchedCreate(
            this: unknown,
            ...args: Parameters<ResponsesCreateType>
          ) {
            const body = args[0];
            const { input: _messages, ...invocationParameters } = body;
            const span = instrumentation.fiTracer.startSpan(
              `OpenAI Responses`,
              {
                kind: SpanKind.INTERNAL,
                attributes: {
                  [SemanticConventions.FI_SPAN_KIND]:
                    FISpanKind.LLM,
                  [SemanticConventions.LLM_MODEL_NAME]: body.model,
                  [SemanticConventions.INPUT_VALUE]: JSON.stringify(body),
                  [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
                  [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                    JSON.stringify(invocationParameters),
                  [SemanticConventions.LLM_PROVIDER]: LLMProvider.OPENAI,
                  [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
                  ...getResponsesInputMessagesAttributes(body),
                  ...getLLMToolsJSONSchema(body),
                  [SemanticConventions.RAW_INPUT]: safelyJSONStringify(body) ?? "",
                },
              },
            );
            const execContext = getExecContext(span);
            const execPromise = safeExecuteInTheMiddle<
              ReturnType<ResponsesCreateType>
            >(
              () => {
                return context.with(trace.setSpan(execContext, span), () => {
                  return original.apply(this, args);
                });
              },
              (error) => {
                // Push the error to the span
                if (error) {
                  span.recordException(error);
                  span.setStatus({
                    code: SpanStatusCode.ERROR,
                    message: error.message,
                  });
                  span.end();
                }
              },
            );
            const wrappedPromise = execPromise.then((result) => {
              const recordSpan = (result?: ResponseType) => {
                if (!result) {
                  span.setStatus({ code: SpanStatusCode.ERROR });
                  span.end();
                  return;
                }
                span.setAttributes({
                  [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
                  [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
                  [SemanticConventions.LLM_MODEL_NAME]: result.model,
                  [SemanticConventions.GEN_AI_RESPONSE_MODEL]: result.model,
                  [SemanticConventions.GEN_AI_RESPONSE_ID]: result.id,
                  ...getResponsesOutputMessagesAttributes(result),
                  ...getResponsesUsageAttributes(result),
                  [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(result) ?? "",
                });
                span.setStatus({ code: SpanStatusCode.OK });
                span.end();
              };
              if (isResponseCreateResponse(result)) {
                recordSpan(result);
              } else {
                const [leftStream, rightStream] = result.tee();
                consumeResponseStreamEvents(rightStream).then(recordSpan);
                result = leftStream;
              }

              return result;
            }).catch((error: Error) => {
              span.recordException(error);
              span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
              span.end();
              throw error;
            });
            return context.bind(execContext, wrappedPromise);
          };
        },
      );
    }

    _isFIPatched = true;
    try {
      // This can fail if the module is made immutable via the runtime or bundler
      module.fiPatched = true;
    } catch (e) {
      // diag.debug(`Failed to set ${MODULE_NAME} patched flag on the module`, e);
      diag.warn(`Failed to set fiPatched flag on module '${MODULE_NAME}'. This is usually not an issue. Error: ${e}`);
    }

    return module;
  }
  /**
   * Un-patches the OpenAI module's chat completions API
   */
  private unpatch(
    moduleExports: typeof openai & { fiPatched?: boolean },
    moduleVersion?: string,
  ) {
    // diag.debug(`Removing patch for ${MODULE_NAME}@${moduleVersion}`);
    this._unwrap(moduleExports.OpenAI.Chat.Completions.prototype, "create");
    this._unwrap(moduleExports.OpenAI.Completions.prototype, "create");
    this._unwrap(moduleExports.OpenAI.Embeddings.prototype, "create");

    _isFIPatched = false;
    try {
      // This can fail if the module is made immutable via the runtime or bundler
      moduleExports.fiPatched = false;
    } catch (e) {
      diag.warn(`Failed to unset fiPatched flag on module '${MODULE_NAME}'. This is usually not an issue. Error: ${e}`);
    }
  }
}

function isResponseCreateResponse(
  response: Stream<ResponseStreamEvent> | ResponseType,
): response is ResponseType {
  return "object" in response && response.object === "response";
}

/**
 * type-guard that checks if the response is a chat completion response
 */
function isChatCompletionResponse(
  response: Stream<ChatCompletionChunk> | ChatCompletion,
): response is ChatCompletion {
  return "choices" in response;
}

/**
 * type-guard that checks if the response is a completion response
 */
function isCompletionResponse(
  response: Stream<Completion> | Completion,
): response is Completion {
  return "choices" in response;
}

/**
 * type-guard that checks if completion prompt attribute is an array of strings
 */
function isPromptStringArray(
  prompt: CompletionCreateParamsBase["prompt"],
): prompt is Array<string> {
  return (
    Array.isArray(prompt) && prompt.every((item) => typeof item === "string")
  );
}

/**
 * Converts the body of a chat completions request to LLM input messages (JSON blob)
 */
function getLLMInputMessagesAttributes(
  body: ChatCompletionCreateParamsBase,
): Attributes {
  const messages = body.messages.map((message) => {
    return serializeChatCompletionInputMessage(message);
  });
  return {
    [SemanticConventions.LLM_INPUT_MESSAGES]: safelyJSONStringify(messages) ?? "[]",
  };
}

/**
 * Converts tool definitions into a JSON blob
 */
function getLLMToolsJSONSchema(
  body: ChatCompletionCreateParamsBase | ResponseCreateParamsBase,
): Attributes {
  if (!body.tools) {
    return {};
  }
  return {
    [SemanticConventions.LLM_TOOLS]: safelyJSONStringify(body.tools) ?? "[]",
  };
}

/**
 * Serializes a ChatCompletionMessageParam into a plain object for JSON blob format.
 */
function serializeChatCompletionInputMessage(
  message: ChatCompletionMessageParam,
): Record<string, unknown> {
  const role = message.role;
  const obj: Record<string, unknown> = { role };

  if (typeof message.content === "string") {
    obj.content = message.content;
  } else if (Array.isArray(message.content)) {
    obj.content = message.content.map((part) => {
      if (part.type === "text") {
        return { type: "text", text: part.text };
      } else if (part.type === "image_url") {
        return { type: "image_url", image_url: { url: part.image_url.url } };
      }
      return { type: part.type };
    });
  }

  switch (role) {
    case "assistant":
      if (message.tool_calls) {
        obj.tool_calls = message.tool_calls.map((tc) => ({
          id: tc.id,
          type: tc.type,
          function: { name: tc.function.name, arguments: tc.function.arguments },
        }));
      }
      break;
    case "function":
      obj.name = message.name;
      break;
    case "tool":
      if (message.tool_call_id) {
        obj.tool_call_id = message.tool_call_id;
      }
      break;
    case "user":
    case "system":
    case "developer":
      break;
    default:
      assertUnreachable(role);
      break;
  }
  return obj;
}

/**
 * Converts the body of a completions request to input attributes
 */
function getCompletionInputValueAndMimeType(
  body: CompletionCreateParamsBase,
): Attributes {
  if (typeof body.prompt === "string") {
    return {
      [SemanticConventions.INPUT_VALUE]: body.prompt,
      [SemanticConventions.INPUT_MIME_TYPE]: MimeType.TEXT,
    };
  } else if (isPromptStringArray(body.prompt)) {
    const prompt = body.prompt[0]; // Only single prompts are currently supported
    if (prompt === undefined) {
      return {};
    }
    return {
      [SemanticConventions.INPUT_VALUE]: prompt,
      [SemanticConventions.INPUT_MIME_TYPE]: MimeType.TEXT,
    };
  }
  // Other cases in which the prompt is a token or array of tokens are currently unsupported
  return {};
}

/**
 * Get usage attributes
 */
function getUsageAttributes(
  completion: ChatCompletion | Completion,
): Attributes {
  if (completion.usage) {
    const usageAttributes: Attributes = {
      [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]:
        completion.usage.completion_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_PROMPT]:
        completion.usage.prompt_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_TOTAL]:
        completion.usage.total_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ]:
        completion.usage.prompt_tokens_details?.cached_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_PROMPT_DETAILS_AUDIO]:
        completion.usage.prompt_tokens_details?.audio_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION_DETAILS_AUDIO]:
        completion.usage.completion_tokens_details?.audio_tokens,
      [SemanticConventions.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING]:
        completion.usage.completion_tokens_details?.reasoning_tokens,
    };
    return usageAttributes;
  }
  return {};
}

/**
 * Converts the chat completion result to LLM output messages (JSON blob)
 */
function getChatCompletionLLMOutputMessagesAttributes(
  chatCompletion: ChatCompletion,
): Attributes {
  const choice = chatCompletion.choices[0];
  if (!choice) {
    return {};
  }
  const messages = [serializeChatCompletionOutputMessage(choice.message)];
  return {
    [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify(messages) ?? "[]",
  };
}

/**
 * Serializes a ChatCompletionMessage into a plain object for JSON blob format.
 */
function serializeChatCompletionOutputMessage(
  message: ChatCompletionMessage,
): Record<string, unknown> {
  const obj: Record<string, unknown> = { role: message.role };
  if (message.content) {
    obj.content = message.content;
  }
  if (message.tool_calls) {
    obj.tool_calls = message.tool_calls.map((tc) => ({
      id: tc.id,
      type: tc.type,
      function: { name: tc.function.name, arguments: tc.function.arguments },
    }));
  }
  if (message.function_call) {
    obj.function_call = {
      name: message.function_call.name,
      arguments: message.function_call.arguments,
    };
  }
  return obj;
}

/**
 * Converts the completion result to output attributes
 */
function getCompletionOutputValueAndMimeType(
  completion: Completion,
): Attributes {
  // Right now support just the first choice
  const choice = completion.choices[0];
  if (!choice) {
    return {};
  }
  return {
    [SemanticConventions.OUTPUT_VALUE]: String(choice.text),
    [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
  };
}

/**
 * Converts the embedding result payload to embedding attributes
 */
function getEmbeddingTextAttributes(
  request: EmbeddingCreateParams,
): Attributes {
  if (typeof request.input === "string") {
    return {
      [`${SemanticConventions.EMBEDDING_EMBEDDINGS}.0.${SemanticConventions.EMBEDDING_TEXT}`]:
        request.input,
    };
  } else if (
    Array.isArray(request.input) &&
    request.input.length > 0 &&
    typeof request.input[0] === "string"
  ) {
    return request.input.reduce((acc, input, index) => {
      const indexPrefix = `${SemanticConventions.EMBEDDING_EMBEDDINGS}.${index}.`;
      acc[`${indexPrefix}${SemanticConventions.EMBEDDING_TEXT}`] = input;
      return acc;
    }, {} as Attributes);
  }
  // Ignore other cases where input is a number or an array of numbers
  return {};
}

/**
 * Converts the embedding result payload to embedding attributes
 */
function getEmbeddingEmbeddingsAttributes(
  response: CreateEmbeddingResponse,
): Attributes {
  return response.data.reduce((acc, embedding, index) => {
    const indexPrefix = `${SemanticConventions.EMBEDDING_EMBEDDINGS}.${index}.`;
    acc[`${indexPrefix}${SemanticConventions.EMBEDDING_VECTOR}`] =
      embedding.embedding;
    return acc;
  }, {} as Attributes);
}

/**
 * Consumes the stream chunks and adds them to the span
 */
async function consumeChatCompletionStreamChunks(
  stream: Stream<ChatCompletionChunk>,
  span: Span,
) {
  // --- ADD LOG ---
  // diag.debug(`@traceai/openai: consumeChatCompletionStreamChunks CALLED for span: ${span.spanContext().spanId}`);
  let streamResponse = "";
  // Tool and function call attributes can also arrive in the stream
  // NB: the tools and function calls arrive in partial diffs
  // So the final tool and function calls need to be aggregated
  // across chunks
  const toolAndFunctionCallAttributes: Attributes = {};
  // The first message is for the assistant response so we start at 1
  const allChunks: ChatCompletionChunk[] = [];
  for await (const chunk of stream) {
    allChunks.push(chunk); // Store all chunks
    if (chunk.choices.length <= 0) {
      continue;
    }
    const choice = chunk.choices[0];
    if (choice.delta.content) {
      streamResponse += choice.delta.content;
    }
    // Accumulate the tool and function call attributes
    const toolAndFunctionCallAttributesDiff =
      getToolAndFunctionCallAttributesFromStreamChunk(chunk);
    for (const [key, value] of Object.entries(
      toolAndFunctionCallAttributesDiff,
    )) {
      if (isString(toolAndFunctionCallAttributes[key]) && isString(value)) {
        toolAndFunctionCallAttributes[key] += value;
      } else if (isString(value)) {
        toolAndFunctionCallAttributes[key] = value;
      }
    }
  }
  // Build the output message as a JSON blob
  const outputMessage: Record<string, unknown> = {
    role: "assistant",
    content: streamResponse,
  };
  // Aggregate tool calls from stream attributes into structured format
  const toolCalls: Record<number, Record<string, unknown>> = {};
  for (const [key, value] of Object.entries(toolAndFunctionCallAttributes)) {
    // Parse tool call attributes from the nested format
    const toolCallMatch = key.match(/^message\.tool_calls\.(\d+)\./);
    if (toolCallMatch) {
      const idx = parseInt(toolCallMatch[1]);
      if (!toolCalls[idx]) toolCalls[idx] = {};
      if (key.endsWith("tool_call.id")) toolCalls[idx].id = value;
      if (key.endsWith("tool_call.function.name")) {
        if (!toolCalls[idx].function) toolCalls[idx].function = {};
        (toolCalls[idx].function as Record<string, unknown>).name = value;
      }
      if (key.endsWith("tool_call.function.arguments")) {
        if (!toolCalls[idx].function) toolCalls[idx].function = {};
        (toolCalls[idx].function as Record<string, unknown>).arguments = value;
      }
    }
    // Handle legacy function_call
    if (key === SemanticConventions.MESSAGE_FUNCTION_CALL_NAME) {
      outputMessage.function_call_name = value;
    }
    if (key === SemanticConventions.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON) {
      outputMessage.function_call_arguments = value;
    }
  }
  const toolCallArray = Object.values(toolCalls);
  if (toolCallArray.length > 0) {
    outputMessage.tool_calls = toolCallArray;
  }

  const attributes: Attributes = {
    [SemanticConventions.OUTPUT_VALUE]: streamResponse,
    [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
    [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([outputMessage]) ?? "[]",
    [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(allChunks) ?? "",
  };
  span.setAttributes(attributes);
  span.end(); // Streaming end
  // --- ADD LOG ---
  // diag.debug(`@traceai/openai: consumeChatCompletionStreamChunks span ENDED: ${span.spanContext().spanId}`);
}

/**
 * Extracts the semantic attributes from the stream chunk for tool_calls and function_calls
 */
function getToolAndFunctionCallAttributesFromStreamChunk(
  chunk: ChatCompletionChunk,
): Attributes {
  if (chunk.choices.length <= 0) {
    return {};
  }
  const choice = chunk.choices[0];
  const attributes: Attributes = {};
  if (choice.delta.tool_calls) {
    choice.delta.tool_calls.forEach((toolCall, index) => {
      const toolCallIndexPrefix = `${SemanticConventions.MESSAGE_TOOL_CALLS}.${index}.`;
      // Add the tool call id if it exists
      if (toolCall.id) {
        attributes[
          `${toolCallIndexPrefix}${SemanticConventions.TOOL_CALL_ID}`
        ] = toolCall.id;
      }
      // Double check that the tool call has a function
      // NB: OpenAI only supports tool calls with functions right now but this may change
      if (toolCall.function) {
        attributes[
          toolCallIndexPrefix + SemanticConventions.TOOL_CALL_FUNCTION_NAME
        ] = toolCall.function.name;
        attributes[
          toolCallIndexPrefix +
            SemanticConventions.TOOL_CALL_FUNCTION_ARGUMENTS_JSON
        ] = toolCall.function.arguments;
      }
    });
  }
  if (choice.delta.function_call) {
    attributes[SemanticConventions.MESSAGE_FUNCTION_CALL_NAME] =
      choice.delta.function_call.name;
    attributes[SemanticConventions.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON] =
      choice.delta.function_call.arguments;
  }
  return attributes;
}