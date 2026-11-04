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
import { providerForResource } from "./provider";
import { observeAPIPromise } from "./apiPromise";
import {
  SemanticConventions,
  FISpanKind,
  MimeType,
  LLMSystem,
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
import { assertUnreachable } from "./typeUtils";
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

// Key by the concrete SDK class, not its mutable/immutable module namespace.
// CJS and ESM exports can be distinct classes in the same process.
const patchOwners = new WeakMap<object, OpenAIInstrumentation>();
let patchedSDKCount = 0;

/** Whether any SDK class is currently patched by this module. */
export function isPatched() {
  return patchedSDKCount > 0;
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
  // Lazily initialized: InstrumentationBase can patch during super().
  // A Set is needed so disable() also undoes every manually patched ESM SDK.
  private patchedModules?: Set<typeof openai>;

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
    // The base constructor can call enable() before our trace options exist.
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  protected init() {
    const module = new InstrumentationNodeModuleDefinition(
      "openai",
      ["^4.0.0", "^5.0.0", "^6.0.0", "^7.0.0"],
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
    this.patch(module);
  }

  public override disable(): void {
    super.disable();
    for (const module of this.patchedModules ?? []) {
      this.unpatch(module);
    }
  }

  /**
   * Patches the OpenAI module
   */
  private patch(
    module: typeof openai,
    moduleVersion?: string,
  ) {
    // diag.debug(`Applying patch for ${MODULE_NAME}@${moduleVersion}`);
    const sdk = module?.OpenAI;
    if (sdk && patchOwners.has(sdk)) {
      return module;
    }
    const resources = [
      module?.OpenAI?.Chat?.Completions,
      module?.OpenAI?.Completions,
      module?.OpenAI?.Embeddings,
      ...(module?.OpenAI?.Responses ? [module.OpenAI.Responses] : []),
    ];
    if (resources.some(resource => typeof resource?.prototype?.create !== "function")) {
      diag.warn("@traceai/openai: unsupported SDK module shape; instrumentation was not applied");
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
    if (!(instrumentation.fiTracer as any)?.tracer || (instrumentation.fiTracer as any).tracer !== instrumentation.tracer) {
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
          // Prediction text is input content, captured only in maskable payloads.
          const { messages: _messages, prediction: _prediction, ...invocationParameters } = body;

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
                [SemanticConventions.LLM_PROVIDER]: providerForResource(this),
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

          const wrappedPromise = observeAPIPromise(execPromise, (result) => {
            if (!span.isRecording()) return result;
            if (isChatCompletionResponse(result)) {
              span.setAttributes({
                [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
                [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
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
              result = observeStream(result, span, (chunks) =>
                consumeChatCompletionStreamChunks(chunks, span),
                args[1]?.signal,
              );
            }
            return result;
          }, (error) => endSpanWithError(span, error), (response) => endRawResponseSpan(span, response));
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
          // Suffix is input text, not an unmasked tuning parameter.
          const { prompt: _prompt, suffix: _suffix, ...invocationParameters } = body;
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
                [SemanticConventions.LLM_PROVIDER]: providerForResource(this),
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
          const wrappedPromise = observeAPIPromise(execPromise, (result) => {
            if (!span.isRecording()) return result;
            if (isCompletionResponse(result)) {
              span.setAttributes({
                [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(result),
                [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
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
            } else {
              result = observeStream(result, span, (chunks) => {
                span.setAttributes({
                  ...getStreamMetadataAttributes(chunks),
                  [SemanticConventions.OUTPUT_VALUE]: chunks
                    .map((chunk) => chunk.choices.find((choice) => choice.index === 0)?.text ?? "")
                    .join(""),
                  [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
                  [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(chunks) ?? "",
                });
                span.setStatus({ code: SpanStatusCode.OK });
              }, args[1]?.signal);
            }
            return result;
          }, (error) => endSpanWithError(span, error), (response) => endRawResponseSpan(span, response));
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
              [SemanticConventions.LLM_PROVIDER]: providerForResource(this),
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
          const wrappedPromise = observeAPIPromise(execPromise, (result) => {
            if (!span.isRecording()) return result;
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
          }, (error) => endSpanWithError(span, error), (response) => endRawResponseSpan(span, response));
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
            // Stored prompts are declared by SDK releases newer than our build SDK.
            const body = args[0] as ResponseCreateParamsBase & {
              prompt?: { id: string; version?: string; variables?: Record<string, unknown> };
            };
            // Telemetry-only copy: MCP credentials must never reach any exported
            // duplicate, but the original SDK request must remain untouched.
            const telemetryBody = {
              ...body,
              ...(body.tools && { tools: sanitizeToolDefinitions(body.tools) }),
              ...(body.input && { input: sanitizeResponsesInput(body.input) }),
            };
            const { input: _messages, instructions: _instructions, prompt, ...parameters } = telemetryBody;
            const invocationParameters = {
              ...parameters,
              ...(prompt && { prompt: { id: prompt.id, version: prompt.version } }),
            };
            const span = instrumentation.fiTracer.startSpan(
              `OpenAI Responses`,
              {
                kind: SpanKind.INTERNAL,
                attributes: {
                  [SemanticConventions.FI_SPAN_KIND]:
                    FISpanKind.LLM,
                  [SemanticConventions.LLM_MODEL_NAME]: body.model,
                  [SemanticConventions.INPUT_VALUE]: JSON.stringify(telemetryBody),
                  [SemanticConventions.INPUT_MIME_TYPE]: MimeType.JSON,
                  [SemanticConventions.LLM_INVOCATION_PARAMETERS]:
                    JSON.stringify(invocationParameters),
                  [SemanticConventions.PROMPT_TEMPLATE_VARIABLES]: safelyJSONStringify(prompt?.variables) ?? undefined,
                  [SemanticConventions.LLM_PROVIDER]: providerForResource(this),
                  [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
                  ...getResponsesInputMessagesAttributes(body),
                  ...getLLMToolsJSONSchema(body),
                  [SemanticConventions.RAW_INPUT]: safelyJSONStringify(telemetryBody) ?? "",
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
            const wrappedPromise = observeAPIPromise(execPromise, (result) => {
              if (!span.isRecording()) return result;
              const recordSpan = (result?: ResponseType, endSpan = true) => {
                if (!result) {
                  span.setStatus({ code: SpanStatusCode.ERROR });
                  if (endSpan) span.end();
                  return;
                }
                const output = {
                  id: result.id, object: result.object, status: result.status,
                  model: result.model, output: result.output, usage: result.usage,
                };
                span.setAttributes({
                  [SemanticConventions.OUTPUT_VALUE]: JSON.stringify(output),
                  [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
                  [SemanticConventions.GEN_AI_RESPONSE_MODEL]: result.model,
                  [SemanticConventions.GEN_AI_RESPONSE_ID]: result.id,
                  ...getResponsesOutputMessagesAttributes(result),
                  ...getResponsesUsageAttributes(result),
                  [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(output) ?? "",
                });
                if (["failed", "incomplete", "cancelled"].includes(result.status ?? "") || result.error) {
                  const message = result.error?.message ?? `Response ${result.status}`;
                  span.recordException(new Error(message));
                  span.setStatus({ code: SpanStatusCode.ERROR, message });
                } else {
                  span.setStatus({ code: SpanStatusCode.OK });
                }
                if (endSpan) span.end();
              };
              if (isResponseCreateResponse(result)) {
                recordSpan(result);
              } else {
                result = observeStream(
                  result,
                  span,
                  async (chunks) => {
                    recordSpan(await consumeResponseStreamEvents(chunks), false);
                  },
                  args[1]?.signal,
                  (event) => event.type === "response.completed" || event.type === "response.failed" || event.type === "response.incomplete",
                );
              }

              return result;
            }, (error) => endSpanWithError(span, error), (response) => endRawResponseSpan(span, response));
            return context.bind(execContext, wrappedPromise);
          };
        },
      );
    }

    // Keep the legacy diagnostic property, but never use it as the guard.
    // Immutable class exports still work via the identity map.
    try { (sdk as typeof openai & { fiPatched?: boolean }).fiPatched = true; } catch { /* immutable SDK */ }
    patchOwners.set(sdk, this);
    (this.patchedModules ??= new Set()).add(module);
    patchedSDKCount++;

    return module;
  }
  /**
   * Un-patches the OpenAI module's chat completions API
   */
  private unpatch(moduleExports: typeof openai, moduleVersion?: string) {
    const sdk = moduleExports?.OpenAI;
    if (!sdk || patchOwners.get(sdk) !== this) return;
    this._unwrap(sdk.Chat.Completions.prototype, "create");
    this._unwrap(sdk.Completions.prototype, "create");
    this._unwrap(sdk.Embeddings.prototype, "create");
    if (sdk.Responses) this._unwrap(sdk.Responses.prototype, "create");
    try { (sdk as typeof openai & { fiPatched?: boolean }).fiPatched = false; } catch { /* immutable SDK */ }
    patchOwners.delete(sdk);
    // An automatic require hook may use a different namespace for this class.
    for (const module of this.patchedModules ?? []) {
      if (module.OpenAI === sdk) this.patchedModules!.delete(module);
    }
    patchedSDKCount--;
  }

}

/** Observe the caller's iterator, including errors and early return, without a
 * background tee that can lose errors, keep requests alive or require newer SDKs.
 * Keep the original Stream object, controller and SDK helpers intact.
 */
function observeStream<T>(
  stream: Stream<T>,
  span: Span,
  onComplete: (chunks: T[]) => void | Promise<void>,
  requestSignal?: AbortSignal | null,
  retainChunk: (chunk: T) => boolean = () => true,
): Stream<T> {
  // Modern SDK helpers (notably tee) call the iterator factory directly.
  // OpenAI 4.0 instead implements only Symbol.asyncIterator.
  const sdkStream = stream as unknown as { iterator?: () => AsyncIterator<T> };
  const hasIteratorFactory = typeof sdkStream.iterator === "function";
  const originalIterator = hasIteratorFactory
    ? sdkStream.iterator!.bind(stream)
    : stream[Symbol.asyncIterator].bind(stream);
  const signals = [stream.controller?.signal, requestSignal].filter(
    (signal): signal is AbortSignal => signal != null,
  );
  let ended = false;
  let awaitingNext = false;
  const finish = () => {
    if (ended) return;
    ended = true;
    for (const signal of signals) signal.removeEventListener("abort", onAbort);
    span.end();
  };
  const onAbort = () => {
    if (ended) return;
    // The SDK swallows AbortError and does not forward the request's reason to
    // its own controller. Inspect the original signal before treating EOF as OK.
    const deadline = signals.find(signal => signal.aborted && signal.reason?.name === "TimeoutError");
    if (deadline) {
      span.recordException(deadline.reason);
      span.setStatus({ code: SpanStatusCode.ERROR, message: deadline.reason.message });
    }
    // An SDK parser/transport error can abort its controller in finally before
    // next() rejects. Let that rejection reach our catch before ending the span.
    if (!deadline && awaitingNext) return;
    // Other aborts include the SDK's automatic cancellation on a consumer's
    // early return. End them as UNSET, not successful generations or failures.
    finish();
  };
  for (const signal of signals) signal.addEventListener("abort", onAbort, { once: true });
  if (signals.some(signal => signal.aborted)) onAbort();
  const observedIterator = async function* () {
    const chunks: T[] = [];
    try {
      const source = {
        [Symbol.asyncIterator]: () => {
          const iterator = originalIterator();
          return {
            next: async () => {
              awaitingNext = true;
              try { return await iterator.next(); }
              finally { awaitingNext = false; }
            },
            return: iterator.return?.bind(iterator),
            throw: iterator.throw?.bind(iterator),
          };
        },
      };
      for await (const chunk of source) {
        if (!ended && retainChunk(chunk)) chunks.push(chunk);
        yield chunk;
      }
      if (signals.some(signal => signal.aborted)) onAbort();
      if (!ended) await onComplete(chunks);
    } catch (error) {
      if (!ended) {
        const exception = error instanceof Error ? error : new Error(String(error));
        span.recordException(exception);
        span.setStatus({ code: SpanStatusCode.ERROR, message: exception.message });
      }
      throw error;
    } finally {
      finish();
    }
  };
  if (hasIteratorFactory) sdkStream.iterator = observedIterator;
  else stream[Symbol.asyncIterator] = observedIterator;
  return stream;
}

function endSpanWithError(span: Span, error: unknown): void {
  if (!span.isRecording()) return;
  const exception = error instanceof Error ? error : String(error);
  span.recordException(exception);
  span.setStatus({
    code: SpanStatusCode.ERROR,
    message: error instanceof Error ? error.message : String(error),
  });
  span.end();
}

function endRawResponseSpan(span: Span, response: { status: number }): void {
  if (!span.isRecording()) return;
  // Raw callers own the body. Record only HTTP metadata, never consume it to
  // recover output/tokens. This also handles raw streaming responses lazily.
  span.setAttribute("http.response.status_code", response.status);
  span.setStatus({ code: SpanStatusCode.OK });
  span.end();
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

/** URL userinfo, path, query and fragment may carry credentials. Export only the origin. */
function sanitizeMcpServerURL(serverURL: string): string {
  try {
    const url = new URL(serverURL);
    if (url.protocol !== "https:" && url.protocol !== "http:") return "[REDACTED]";
    // Paths can themselves embed API tokens. Only the server origin is safe
    // as telemetry metadata; the SDK still receives the original full URL.
    return url.origin;
  } catch {
    return "[REDACTED]";
  }
}

/** Remove MCP credentials from telemetry without mutating transport objects. */
function sanitizeToolDefinitions(tools: { type: string }[]): { type: string }[] {
  return tools.map((tool) => {
    if (tool.type !== "mcp") return tool;
    // MCP arrived after the SDK build floor. All headers are omitted because
    // custom header names can carry credentials too.
    const { authorization: _authorization, headers: _headers, ...definition } =
      tool as { type: string; authorization?: string; headers?: Record<string, string> };
    return {
      ...definition,
      ...("server_url" in definition && typeof definition.server_url === "string" && {
        server_url: sanitizeMcpServerURL(definition.server_url),
      }),
    };
  });
}

/** SDK 4 predates tool search; sanitize its loaded definitions when present. */
function sanitizeResponsesInput(
  input: ResponseCreateParamsBase["input"],
): ResponseCreateParamsBase["input"] {
  if (!Array.isArray(input)) return input;
  return input.map((item) => {
    if (item && typeof item === "object") {
      const loaded = item as { type: string; tools?: { type: string }[] };
      if (loaded.type === "tool_search_output" && Array.isArray(loaded.tools)) {
        return { ...item, tools: sanitizeToolDefinitions(loaded.tools) } as unknown as typeof item;
      }
    }
    return item;
  });
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
    [SemanticConventions.LLM_TOOLS]: safelyJSONStringify(sanitizeToolDefinitions(body.tools)) ?? "[]",
  };
}

// Structural shape keeps the build compatible with SDK 4 while accepting the
// discriminated custom-tool union introduced in later SDKs. Unknown variants
// retain identity only; tracing must not make a valid SDK request throw.
function serializeToolCall(tc: {
  id: string;
  type: string;
  function?: { name: string; arguments: string };
  custom?: { name: string; input: string };
}): Record<string, unknown> {
  const result: Record<string, unknown> = { id: tc.id, type: tc.type };
  if (tc.type === "function" && tc.function) {
    result.function = { name: tc.function.name, arguments: tc.function.arguments };
  } else if (tc.type === "custom" && tc.custom) {
    result.custom = { name: tc.custom.name, input: tc.custom.input };
  }
  return result;
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
        obj.tool_calls = message.tool_calls.map(serializeToolCall);
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
  completion: Pick<ChatCompletionChunk, "usage">,
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

/** Usage-only final chunks have no choices. Collect metadata independently of deltas. */
function getStreamMetadataAttributes(chunks: (ChatCompletionChunk | Completion)[]): Attributes {
  const attributes: Attributes = {};
  const finishReasons = new Map<number, string>();
  for (const chunk of chunks) {
    if (chunk.model) {
      attributes[SemanticConventions.GEN_AI_RESPONSE_MODEL] = chunk.model;
    }
    if (chunk.id) attributes[SemanticConventions.GEN_AI_RESPONSE_ID] = chunk.id;
    if (chunk.usage) Object.assign(attributes, getUsageAttributes(chunk));
    for (const choice of chunk.choices) {
      if (choice.finish_reason) finishReasons.set(choice.index, choice.finish_reason);
    }
  }
  if (finishReasons.size) {
    attributes[SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS] = JSON.stringify([...finishReasons.values()]);
  }
  return attributes;
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
    obj.tool_calls = message.tool_calls.map(serializeToolCall);
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

// Structural union supports modern custom tools while compiling against SDK 4.
type StreamToolCall = {
  index: number;
  id?: string;
  type?: string;
  function?: { name?: string; arguments?: string };
  custom?: { name?: string; input?: string };
};

/** Accumulate one tool by its stable SDK index, never its position in a chunk. */
function accumulateToolCall(
  tools: Map<number, StreamToolCall>,
  delta: StreamToolCall,
): void {
  const tool = tools.get(delta.index) ?? { index: delta.index };
  if (delta.id !== undefined) tool.id = delta.id;
  if (delta.type !== undefined) tool.type = delta.type;
  if (delta.function) {
    tool.function ??= { name: "", arguments: "" };
    tool.function.name = (tool.function.name ?? "") + (delta.function.name ?? "");
    tool.function.arguments = (tool.function.arguments ?? "") + (delta.function.arguments ?? "");
  }
  if (delta.custom) {
    tool.custom ??= { name: "", input: "" };
    tool.custom.name = (tool.custom.name ?? "") + (delta.custom.name ?? "");
    tool.custom.input = (tool.custom.input ?? "") + (delta.custom.input ?? "");
  }
  tools.set(delta.index, tool);
}

/** Consume the first choice, retaining SDK chunks unchanged for the caller. */
async function consumeChatCompletionStreamChunks(
  stream: Iterable<ChatCompletionChunk>,
  span: Span,
) {
  let streamResponse = "";
  const tools = new Map<number, StreamToolCall>();
  let functionName = "";
  let functionArguments = "";
  const allChunks: ChatCompletionChunk[] = [];
  for await (const chunk of stream) {
    allChunks.push(chunk);
    const choice = chunk.choices.find((choice) => choice.index === 0);
    if (!choice) continue;
    streamResponse += choice.delta.content ?? "";
    for (const tool of choice.delta.tool_calls ?? []) {
      accumulateToolCall(tools, tool);
    }
    functionName += choice.delta.function_call?.name ?? "";
    functionArguments += choice.delta.function_call?.arguments ?? "";
  }
  const outputMessage: Record<string, unknown> = {
    role: "assistant",
    content: streamResponse,
  };
  if (tools.size) {
    outputMessage.tool_calls = [...tools.entries()]
      .sort(([a], [b]) => a - b)
      .map(([, { index: _index, ...tool }]) => tool);
  }
  if (functionName) outputMessage.function_call_name = functionName;
  if (functionArguments) outputMessage.function_call_arguments = functionArguments;

  span.setAttributes({
    ...getStreamMetadataAttributes(allChunks),
    [SemanticConventions.OUTPUT_VALUE]: streamResponse,
    [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.TEXT,
    [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([outputMessage]) ?? "[]",
    [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(allChunks) ?? "",
  });
  span.setStatus({ code: SpanStatusCode.OK });
}
