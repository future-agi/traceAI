import Anthropic from "@anthropic-ai/sdk";
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
import { SemanticConventions, FISpanKind, LLMProvider, MimeType } from "@traceai/fi-semantic-conventions";
import { isTracingSuppressed } from "@opentelemetry/core";
import { FITracer, safelyJSONStringify, TraceConfigOptions } from "@traceai/fi-core";
import { VERSION } from "./version";
import {
    getAnthropicInputMessagesAttributes,
    getAnthropicOutputMessagesAttributes,
    getAnthropicUsageAttributes,
    getAnthropicToolsAttributes,
    aggregateAnthropicStreamEvents,
} from "./responseAttributes";
import { type MessageStreamEvent } from "@anthropic-ai/sdk/resources/messages";
import { Stream } from "@anthropic-ai/sdk/streaming";

const MODULE_NAME = "@anthropic-ai/sdk";
let _isFIPatched = false;

export function isPatched() {
  return _isFIPatched;
}

function getExecContext(span: Span) {
  const activeContext = context.active();
  const suppressTracing = isTracingSuppressed(activeContext);
  // If tracing is suppressed, don't set the span in context, effectively dropping it for nested operations
  const execContext = suppressTracing ? activeContext : trace.setSpan(context.active(), span);
  // TODO: check if this diag log is needed or should be removed/conditional
  // if (suppressTracing) {
  //   diag.debug("Tracing is suppressed. Span will not be exported or set in context for children.");
  // }
  return execContext;
}

/**
 * The SDK classes a module object can expose. `require("@anthropic-ai/sdk")` is a callable
 * wrapper whose own `prototype` is a plain object, so also look at `.Anthropic` and `.default`.
 */
function sdkCandidates(moduleExports: any): any[] {
  return [moduleExports, moduleExports?.Anthropic, moduleExports?.default].filter(
    (candidate) => typeof candidate === "function",
  );
}

function hasNativeOpenTelemetry(moduleExports: any): boolean {
  // `in` never calls the client getter, which may read private fields.
  return sdkCandidates(moduleExports).some(
    (candidate) => !!candidate.prototype && "openTelemetry" in candidate.prototype,
  );
}

function getMessagesPrototype(moduleExports: any): any {
  if (moduleExports?.Messages?.prototype) return moduleExports.Messages.prototype;
  for (const candidate of sdkCandidates(moduleExports)) {
    if (candidate.Messages?.prototype) return candidate.Messages.prototype;
  }
  return undefined;
}

/** The SDK's `APIPromise`: a Promise with `withResponse()`, `asResponse()` and lazy parsing. */
interface APIPromiseLike {
  _thenUnwrap(transform: (data: unknown) => unknown): APIPromiseLike;
  responsePromise?: Promise<unknown>;
  parse?: () => Promise<unknown>;
  asResponse?: () => Promise<unknown>;
  then: Promise<unknown>["then"];
}

function isAPIPromise(value: unknown): value is APIPromiseLike {
  return (
    !!value &&
    typeof (value as APIPromiseLike)._thenUnwrap === "function" &&
    typeof (value as APIPromiseLike).then === "function"
  );
}

/**
 * Returns an SDK APIPromise that records the call when the application reads it. The SDK
 * parses the body only when the application asks for it, so this does not read it early.
 */
function instrumentAPIPromise(
  apiPromise: APIPromiseLike,
  onResult: (data: unknown) => unknown,
  onError: (error: unknown) => void,
  onRawResponse: () => void,
): APIPromiseLike {
  // Request and API errors reject here, whichever helper the application uses. Chain the
  // observer into the promise the SDK reads, and rethrow, instead of attaching a side
  // handler: a side handler would mark the rejection handled, so an application that
  // drops a failing call would no longer get Node's unhandled rejection.
  // _thenUnwrap() reads responsePromise, so replace it first.
  if (apiPromise.responsePromise) {
    apiPromise.responsePromise = apiPromise.responsePromise.then(
      (value) => value,
      (error) => {
        onError(error);
        throw error;
      },
    );
  }

  const instrumented = apiPromise._thenUnwrap(onResult);
  let parseRequested = false;
  const originalParse = instrumented.parse;
  if (typeof originalParse === "function") {
    instrumented.parse = function (this: APIPromiseLike) {
      parseRequested = true;
      const parsed = originalParse.call(this);
      parsed.then(undefined, onError);
      return parsed;
    };
  }
  const originalAsResponse = instrumented.asResponse;
  if (typeof originalAsResponse === "function" && typeof originalParse === "function") {
    instrumented.asResponse = function (this: APIPromiseLike) {
      const response = originalAsResponse.call(this);
      // withResponse() parses first; asResponse() alone leaves the body unread.
      // Return the chained promise so a dropped failure still reaches the application.
      // The responsePromise chain above already records errors.
      if (parseRequested) return response;
      return response.then((raw) => {
        if (!parseRequested) onRawResponse();
        return raw;
      });
    };
  }
  return instrumented;
}

/** Record only events the application consumed; never drain a background tee. */
function observeStream(span: Span, stream: Stream<MessageStreamEvent>): Stream<MessageStreamEvent> {
  const chunks: MessageStreamEvent[] = [];
  let sawMessageStop = false;
  let ended = false;
  let pending = 0;
  const signal = stream.controller?.signal;
  const finish = (canceled = false, error?: unknown) => {
    if (ended) return;
    ended = true;
    signal?.removeEventListener("abort", onAbort);
    try {
      finishInstrumentationStream(span, chunks, sawMessageStop, canceled, error);
    } catch {
      // A processor/exporter failure must not replace an SDK iterator result.
    } finally {
      chunks.length = 0;
    }
  };
  // The SDK also aborts in its generator's finally on an API error. Wait for
  // an in-flight operation before deciding whether this was cancellation.
  const onAbort = () => { if (!pending) finish(true); };
  signal?.addEventListener("abort", onAbort, { once: true });
  if (signal?.aborted) finish(true);

  const originalIterator = stream[Symbol.asyncIterator].bind(stream);
  let iteratorObserved = false;
  stream[Symbol.asyncIterator] = () => {
    const iterator = originalIterator();
    // Only the first iterator owns the span. The SDK rejects subsequent consumers;
    // their errors must not end the active iterator's span or discard its output.
    if (iteratorObserved) return iterator;
    iteratorObserved = true;
    return {
      async next(...args: [] | [undefined]) {
        pending++;
        try {
          const result = await iterator.next(...args);
          if (result.done) {
            finish(signal?.aborted === true);
          } else if (!ended) {
            chunks.push(result.value);
            if (result.value.type === "message_stop") sawMessageStop = true;
          }
          return result;
        } catch (error) {
          const canceled = signal?.aborted === true &&
            ["AbortError", "APIUserAbortError"].includes((error as Error)?.name);
          finish(canceled, error);
          throw error;
        } finally {
          pending--;
          if (!pending && signal?.aborted) finish(true);
        }
      },
      async return(value?: unknown) {
        pending++;
        try {
          return iterator.return ? await iterator.return(value) : { done: true as const, value };
        } catch (error) {
          finish(false, error);
          throw error;
        } finally {
          pending--;
          finish(true);
        }
      },
      async throw(error?: unknown) {
        pending++;
        try {
          if (iterator.throw) return await iterator.throw(error);
          throw error;
        } finally {
          pending--;
          finish(true);
        }
      },
    };
  };
  // The original object keeps its controller, tee() and toReadableStream().
  // Those helpers all obtain their iterator through the method above.
  return stream;
}

function finishInstrumentationStream(
  span: Span,
  chunks: MessageStreamEvent[],
  sawMessageStop: boolean,
  canceled: boolean,
  error?: unknown,
) {
  try {
    const { reconstructedMessage } = aggregateAnthropicStreamEvents(chunks);

    // Build output message as JSON blob
    const outputMessage: Record<string, unknown> = { role: reconstructedMessage.role };
    let concatenatedStreamText = "";
    const toolCalls: Record<string, unknown>[] = [];

    reconstructedMessage.content.forEach(block => {
        if (block.type === 'text') {
            concatenatedStreamText += (block as Anthropic.TextBlock).text;
        } else if (block.type === 'tool_use') {
            const toolUseBlock = block as Anthropic.ToolUseBlock;
            toolCalls.push({
              function: { name: toolUseBlock.name, arguments: safelyJSONStringify(toolUseBlock.input) ?? "{}" },
            });
        }
    });

    if (concatenatedStreamText) {
      outputMessage.content = concatenatedStreamText;
    }
    if (toolCalls.length > 0) {
      outputMessage.tool_calls = toolCalls;
    }

    const streamSpanAttributes: Attributes = {
      [SemanticConventions.LLM_OUTPUT_MESSAGES]: safelyJSONStringify([outputMessage]) ?? "[]",
    };

    if (concatenatedStreamText) {
        streamSpanAttributes[SemanticConventions.OUTPUT_VALUE] = concatenatedStreamText;
        streamSpanAttributes[SemanticConventions.OUTPUT_MIME_TYPE] = MimeType.TEXT;
    } else {
        streamSpanAttributes[SemanticConventions.OUTPUT_VALUE] = safelyJSONStringify(reconstructedMessage) ?? "";
        streamSpanAttributes[SemanticConventions.OUTPUT_MIME_TYPE] = MimeType.JSON;
    }

    if (reconstructedMessage.model) {
        streamSpanAttributes[SemanticConventions.GEN_AI_RESPONSE_MODEL] = reconstructedMessage.model;
    }
    if (reconstructedMessage.id) {
        streamSpanAttributes[SemanticConventions.GEN_AI_RESPONSE_ID] = reconstructedMessage.id;
    }
    if (reconstructedMessage.stop_reason) {
        streamSpanAttributes[SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS] = JSON.stringify([reconstructedMessage.stop_reason]);
    }

    if (reconstructedMessage.usage) {
        Object.assign(streamSpanAttributes, getAnthropicUsageAttributes(reconstructedMessage));
    }

    span.setAttributes({
        ...streamSpanAttributes,
        [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(reconstructedMessage) ?? "",
    });
  } catch (attributeError: unknown) {
    diag.error("Error recording Anthropic stream attributes", attributeError);
  } finally {
    if (error !== undefined && !canceled) {
      span.recordException(error as Error);
      span.setStatus({ code: SpanStatusCode.ERROR, message: (error as Error)?.message });
    } else if (sawMessageStop) {
      span.setStatus({ code: SpanStatusCode.OK });
    } else if (!canceled) {
      span.setStatus({ code: SpanStatusCode.ERROR, message: "The stream ended before message_stop." });
    }
    // Explicit cancellation is neither a completed message nor a provider error.
    span.end();
  }
}

export class AnthropicInstrumentation extends InstrumentationBase{
  private fiTracer!: FITracer;
  private _traceConfig?: TraceConfigOptions;

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/instrumentation-anthropic", VERSION,Object.assign({}, instrumentationConfig));
    this._traceConfig = traceConfig;
  }

  public override enable(): void {
    super.enable();
    this.fiTracer = new FITracer({ tracer: this.tracer, traceConfig: this._traceConfig });
  }

  protected init(){
    const module = new InstrumentationNodeModuleDefinition(
      MODULE_NAME,
      [">=0.20.0 <1"], // Looser version range based on recent activity
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    return module;
  }

  manuallyInstrument(module: typeof Anthropic) {
    this.patch(module);
  }

  private patch(moduleExports: typeof Anthropic, moduleVersion?: string) {
    // Native SDK tracing owns the APIPromise, stream lifecycle and privacy options.
    // Do not wrap it, even when the client explicitly disables native telemetry.
    // Check the public capability, not just the automatic loader's version gate:
    // manuallyInstrument() bypasses that gate, and subclasses inherit this getter.
    if (hasNativeOpenTelemetry(moduleExports)) {
      diag.warn(
        "@traceai/anthropic: this SDK has native OpenTelemetry support; " +
        "leaving it unpatched. Configure the SDK's openTelemetry option and " +
        "tracer provider directly. traceConfig on this legacy instrumentor " +
        "does not apply to native SDK spans.",
      );
      return moduleExports;
    }

    if ((moduleExports as any).fiPatched || _isFIPatched) {
      return moduleExports;
    }
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    const instrumentation: AnthropicInstrumentation = this;

    if (!(instrumentation.fiTracer as any).tracer || (instrumentation.fiTracer as any).tracer !== instrumentation.tracer) {
        instrumentation.fiTracer = new FITracer({ tracer: instrumentation.tracer, traceConfig: instrumentation._traceConfig });
    }

    const messagesPrototype = getMessagesPrototype(moduleExports);
    if (!messagesPrototype) {
      diag.error("Anthropic.Messages.prototype not found, cannot patch messages.create.");
      return moduleExports;
    }

    // Patch messages.create
    this._wrap(
      messagesPrototype,
      "create",
      (originalCreate: Anthropic.Messages['create']) => {
        // Not async: the SDK returns an APIPromise whose withResponse(), asResponse()
        // and lazy body parsing callers (including messages.stream()) depend on.
        return function patchedMessagesCreate(this: Anthropic.Messages, ...args: Parameters<Anthropic.Messages['create']>) {
          const params = args[0] as Anthropic.MessageCreateParams;
          const { messages: _messages, system: _system, tools: _tools, ...invocationParameters } = params;

          const span = instrumentation.fiTracer.startSpan("Anthropic Messages Create", {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: params.model,
              [SemanticConventions.LLM_PROVIDER]: LLMProvider.ANTHROPIC,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
              [SemanticConventions.LLM_INVOCATION_PARAMETERS]: safelyJSONStringify(invocationParameters) ?? "",
              ...getAnthropicInputMessagesAttributes(params),
              ...getAnthropicToolsAttributes(params),
              [SemanticConventions.RAW_INPUT]: safelyJSONStringify(params) ?? "",
            },
          });

          const execContext = getExecContext(span);

          let ended = false;
          const endWithError = (error: any) => {
            if (ended) return;
            ended = true;
            span.recordException(error);
            span.setStatus({ code: SpanStatusCode.ERROR, message: error?.message });
            span.end();
          };

          const handleResult = (result: unknown) => {
            if (ended) return result;
            if (params.stream === true) {
              const receivedStream = result as Stream<Anthropic.MessageStreamEvent>;
              ended = true;
              return observeStream(span, receivedStream);
            }
            ended = true;
            const messageResponse = result as Anthropic.Message;
            // The call succeeded. Recording its output must not change what the application
            // receives (the SDK resolves an empty body, such as HTTP 204, as null) and must
            // not leave the span open. The SDK's native tracing also only logs this.
            try {
              if (messageResponse !== null && typeof messageResponse === "object") {
                span.setAttributes({
                  ...getAnthropicOutputMessagesAttributes(messageResponse),
                  ...getAnthropicUsageAttributes(messageResponse),
                  [SemanticConventions.LLM_MODEL_NAME]: messageResponse.model,
                  [SemanticConventions.GEN_AI_RESPONSE_MODEL]: messageResponse.model,
                  [SemanticConventions.GEN_AI_RESPONSE_ID]: messageResponse.id,
                  [SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS]: messageResponse.stop_reason
                    ? JSON.stringify([messageResponse.stop_reason]) : "[]",
                  [SemanticConventions.OUTPUT_VALUE]: safelyJSONStringify(messageResponse) ?? "",
                  [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
                  [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(messageResponse) ?? "",
                });
              }
            } catch (attributeError: unknown) {
              diag.error("Error recording the Anthropic response on its span", attributeError);
            }
            span.setStatus({ code: SpanStatusCode.OK });
            span.end();
            return result;
          };

          let result: unknown;
          try {
            result = context.with(execContext, () => originalCreate.apply(this, args));
          } catch (error: any) {
            endWithError(error);
            throw error;
          }

          if (isAPIPromise(result)) {
            return instrumentAPIPromise(result, handleResult, endWithError, () => {
              // Only the raw response was read: there is no parsed output to record.
              if (ended) return;
              ended = true;
              span.setStatus({ code: SpanStatusCode.OK });
              span.end();
            });
          }

          // Not an SDK APIPromise (unknown SDK build or a test double): plain promise.
          return Promise.resolve(result).then(
            (value) => {
              try {
                return handleResult(value);
              } catch (error: any) {
                endWithError(error);
                throw error;
              }
            },
            (error: any) => {
              endWithError(error);
              throw error;
            },
          );
        } as any; // The wrapper returns the SDK's own APIPromise type at runtime
      }
    );

    _isFIPatched = true;
    try {
      (moduleExports as any).fiPatched = true;
    } catch (e) {
      diag.warn(`Failed to set fiPatched flag on module '${MODULE_NAME}'. This is usually not an issue. Error: ${e}`);
    }
    return moduleExports;
  }

  private unpatch(moduleExports: typeof Anthropic & { fiPatched?: boolean }) {
    const messagesPrototype = getMessagesPrototype(moduleExports);
    if (messagesPrototype) {
      this._unwrap(messagesPrototype, "create");
    } else {
      diag.warn("Anthropic.Messages.prototype not found during unpatch, could not unwrap messages.create.");
    }
    _isFIPatched = false;
    try {
      (moduleExports as any).fiPatched = false;
    } catch (e) {
      diag.warn(`Failed to unset fiPatched flag on module '${MODULE_NAME}'. This is usually not an issue. Error: ${e}`);
    }
  }
}
