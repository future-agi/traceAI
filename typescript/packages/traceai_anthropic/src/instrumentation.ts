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
import { FITracer, generateTraceConfig, safelyJSONStringify, TraceConfigOptions } from "@traceai/fi-core";
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
const BEDROCK_MODULE = "@anthropic-ai/bedrock-sdk";
const VERTEX_MODULE = "@anthropic-ai/vertex-sdk";
// Key ownership by the concrete target, not the module namespace: ESM aliases
// can share Messages, and frozen namespaces cannot carry a fiPatched flag.
const patchedMessages = new Map<Anthropic.Messages, {
  owner: AnthropicInstrumentation;
  moduleExports: typeof Anthropic;
  create: Anthropic.Messages["create"];
  generation: symbol;
  /** The hosted-client module whose loader installed this patch, if any. */
  hostedBy?: unknown;
}>();

export function isPatched() {
  return patchedMessages.size > 0;
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

/** The cloud-hosted Anthropic clients share the base SDK's Messages class. */
const PROVIDER_BEDROCK = "aws.bedrock";
const PROVIDER_VERTEX = "gcp.vertex_ai";

/** Which provider a Messages resource talks to, from its owning client. */
function providerOf(messages: unknown): string {
  const client = (messages as { _client?: object } | undefined)?._client;
  if (!client || typeof client !== "object") return LLMProvider.ANTHROPIC;
  const name = (client.constructor as { name?: string } | undefined)?.name;
  if (name === "AnthropicBedrock" || (name !== "AnthropicVertex" && "awsRegion" in client)) return PROVIDER_BEDROCK;
  if (name === "AnthropicVertex" || ("projectId" in client && "region" in client)) return PROVIDER_VERTEX;
  return LLMProvider.ANTHROPIC;
}

/** The module object of a cloud-hosted client package, and how to build a client from it without credentials. */
function hostedClient(moduleExports: any): { className: string; probe: Record<string, unknown> } | undefined {
  if (typeof moduleExports?.AnthropicBedrock === "function") {
    return { className: "AnthropicBedrock", probe: { awsRegion: "us-east-1" } };
  }
  if (typeof moduleExports?.AnthropicVertex === "function") {
    return { className: "AnthropicVertex", probe: { region: "us-east5", projectId: "traceai-probe" } };
  }
  return undefined;
}

/** What the hide settings withhold: raw objects and whole responses are held to the strictest one. */
interface Privacy {
  /** Any hide setting is on: the raw request and response objects are not recorded. */
  hidesAny: boolean;
  /** An input hide setting is on: only response-side fields are recorded as output. */
  hidesInput: boolean;
}

function privacyOf(traceConfig?: TraceConfigOptions): Privacy {
  const config = generateTraceConfig(traceConfig);
  const hidesInput = !!(config.hideInputs || config.hideInputMessages || config.hideInputText || config.hideInputImages);
  const hidesAny = hidesInput || !!(config.hideOutputs || config.hideOutputMessages || config.hideOutputText);
  return { hidesAny, hidesInput };
}

const RESPONSE_FIELDS = ["id", "type", "role", "model", "content", "stop_reason", "stop_sequence", "usage"] as const;

/** The response-side fields of a message; anything else the object carries is left out. */
function responseSide(message: object): Record<string, unknown> {
  const picked: Record<string, unknown> = {};
  for (const field of RESPONSE_FIELDS) {
    if (field in message) picked[field] = (message as Record<string, unknown>)[field];
  }
  return picked;
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
function observeStream(span: Span, stream: Stream<MessageStreamEvent>, privacy: Privacy): Stream<MessageStreamEvent> {
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
      finishInstrumentationStream(span, chunks, sawMessageStop, canceled, privacy, error);
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

  // Legacy SDKs share this factory between Symbol.asyncIterator and tee(). It is
  // private in the SDK's TypeScript declaration, but is a regular instance field.
  // Observing only Symbol.asyncIterator misses tee(), which calls the factory directly.
  const sdkStream = stream as unknown as { iterator?: () => AsyncIterator<MessageStreamEvent> };
  const originalIterator = typeof sdkStream.iterator === "function"
    ? sdkStream.iterator.bind(stream)
    : stream[Symbol.asyncIterator].bind(stream);
  let iteratorObserved = false;
  const observeIterator = () => {
    const iterator = originalIterator();
    let ownsSpan = false;
    let closed = false;
    return {
      [Symbol.asyncIterator]() { return this; },
      async next(...args: [] | [undefined]) {
        // SDK generators claim consumption on first next(), not creation. A return
        // or throw before that closes only this iterator, leaving the stream usable.
        if (!closed && !iteratorObserved) {
          iteratorObserved = true;
          ownsSpan = true;
        }
        // Rejected secondary consumers cannot finish the owner's span or lose usage.
        if (!ownsSpan) return iterator.next(...args);
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
        closed = true;
        if (!ownsSpan) return iterator.return ? iterator.return(value) : { done: true as const, value };
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
        closed = true;
        if (!ownsSpan) {
          if (iterator.throw) return iterator.throw(error);
          throw error;
        }
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
  if (typeof sdkStream.iterator === "function") {
    sdkStream.iterator = observeIterator;
  } else {
    // Preserve support for async iterables without the SDK's factory field.
    stream[Symbol.asyncIterator] = observeIterator;
  }
  // Keep the SDK object, controller and helpers; no iterator is advanced here.
  return stream;
}

function finishInstrumentationStream(
  span: Span,
  chunks: MessageStreamEvent[],
  sawMessageStop: boolean,
  canceled: boolean,
  privacy: Privacy,
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
        streamSpanAttributes[SemanticConventions.OUTPUT_VALUE] = safelyJSONStringify(
          privacy.hidesInput ? responseSide(reconstructedMessage) : reconstructedMessage) ?? "";
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

    // The raw response is not recorded while any hide setting is on.
    span.setAttributes(privacy.hidesAny ? streamSpanAttributes : {
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
  private _manualModules = new Set<typeof Anthropic>();

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
    // InstrumentationBase can call enable() before subclass fields initialize.
    for (const module of this._manualModules ?? []) this.patch(module);
  }

  public override disable(): void {
    super.disable();
    // The automatic loader never sees native-ESM manual targets.
    for (const [messagesPrototype, patch] of patchedMessages) {
      if (patch.owner === this) this.unpatchMessages(messagesPrototype);
    }
  }

  protected init(){
    const module = new InstrumentationNodeModuleDefinition(
      MODULE_NAME,
      [">=0.20.0 <1"], // Looser version range based on recent activity
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
    // The cloud-hosted clients build on the base SDK's Messages class but may be the only
    // module the application loads, so each gets its own definition.
    const hosted = (name: string) =>
      new InstrumentationNodeModuleDefinition(name, [">=0.1.0"], this.patch.bind(this), this.unpatch.bind(this));
    return [module, hosted(BEDROCK_MODULE), hosted(VERTEX_MODULE)];
  }

  /** Patches the Messages class a hosted client uses, found from a client built without credentials. */
  private patchHosted(moduleExports: any, className: string, probe: Record<string, unknown>) {
    const ClientClass = moduleExports?.[className];
    // A client with native OpenTelemetry support traces itself.
    if (ClientClass.prototype && "openTelemetry" in ClientClass.prototype) return moduleExports;
    let messagesPrototype: any;
    try {
      // Constructing a client reads the environment only: it does no I/O and needs no credentials.
      messagesPrototype = Object.getPrototypeOf(new ClientClass(probe).messages);
    } catch (error) {
      diag.warn(`@traceai/anthropic: could not find the Messages class of ${className}`, error);
      return moduleExports;
    }
    if (!messagesPrototype || typeof messagesPrototype.create !== "function") return moduleExports;
    this.patchMessages({ Messages: { prototype: messagesPrototype } } as any, moduleExports);
    return moduleExports;
  }

  private unpatchHosted(moduleExports: any) {
    for (const [messagesPrototype, patch] of patchedMessages) {
      if (patch.owner === this && patch.hostedBy === moduleExports) this.unpatchMessages(messagesPrototype);
    }
  }

  manuallyInstrument(module: typeof Anthropic | object) {
    this._manualModules.add(module as typeof Anthropic);
    if (this.isEnabled()) this.patch(module as typeof Anthropic);
  }

  private patch(moduleExports: typeof Anthropic, moduleVersion?: string) {
    const hosted = hostedClient(moduleExports);
    if (hosted) return this.patchHosted(moduleExports, hosted.className, hosted.probe);
    return this.patchMessages(moduleExports);
  }

  private patchMessages(moduleExports: typeof Anthropic, hostedBy?: unknown) {
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

    const messagesPrototype = getMessagesPrototype(moduleExports);
    if (!messagesPrototype) {
      diag.error("Anthropic.Messages.prototype not found, cannot patch messages.create.");
      return moduleExports;
    }
    if (patchedMessages.has(messagesPrototype) || (moduleExports as any).fiPatched) {
      return moduleExports;
    }
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    const instrumentation: AnthropicInstrumentation = this;

    if (!instrumentation.fiTracer || (instrumentation.fiTracer as any).tracer !== instrumentation.tracer) {
        instrumentation.fiTracer = new FITracer({ tracer: instrumentation.tracer, traceConfig: instrumentation._traceConfig });
    }

    const privacy = privacyOf(instrumentation._traceConfig);
    const previousCreate = messagesPrototype.create;
    const generation = Symbol("messages.create");
    // Patch messages.create
    this._wrap(
      messagesPrototype,
      "create",
      (originalCreate: Anthropic.Messages['create']) => {
        // Not async: the SDK returns an APIPromise whose withResponse(), asResponse()
        // and lazy body parsing callers (including messages.stream()) depend on.
        return function patchedMessagesCreate(this: Anthropic.Messages, ...args: Parameters<Anthropic.Messages['create']>) {
          // Retained wrappers stay inactive even after this instance is re-enabled.
          // Unpatch removes the registration; a later patch gets a new generation.
          if (!instrumentation.isEnabled() || patchedMessages.get(messagesPrototype)?.generation !== generation) {
            return originalCreate.apply(this, args);
          }
          const params = args[0] as Anthropic.MessageCreateParams;
          // Read before the call: a hosted client rewrites the shared request body (model, stream) while building the request.
          const streaming = params.stream === true;
          const { messages: _messages, system: _system, tools: _tools, ...invocationParameters } = params;

          const provider = providerOf(this);
          const span = instrumentation.fiTracer.startSpan("Anthropic Messages Create", {
            kind: SpanKind.INTERNAL,
            attributes: {
              [SemanticConventions.FI_SPAN_KIND]: FISpanKind.LLM,
              [SemanticConventions.LLM_MODEL_NAME]: params.model,
              [SemanticConventions.LLM_PROVIDER]: provider,
              [SemanticConventions.GEN_AI_OPERATION_NAME]: "chat",
              [SemanticConventions.LLM_INVOCATION_PARAMETERS]: safelyJSONStringify(invocationParameters) ?? "",
              ...getAnthropicInputMessagesAttributes(params),
              ...getAnthropicToolsAttributes(params),
              // The raw request is not recorded while any hide setting is on.
              ...(privacy.hidesAny ? {} : { [SemanticConventions.RAW_INPUT]: safelyJSONStringify(params) ?? "" }),
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
            if (streaming) {
              const receivedStream = result as Stream<Anthropic.MessageStreamEvent>;
              ended = true;
              return observeStream(span, receivedStream, privacy);
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
                  // A hosted client's request model is the platform's id; the response names the model itself.
                  ...(provider === LLMProvider.ANTHROPIC ? { [SemanticConventions.LLM_MODEL_NAME]: messageResponse.model } : {}),
                  [SemanticConventions.GEN_AI_RESPONSE_MODEL]: messageResponse.model,
                  [SemanticConventions.GEN_AI_RESPONSE_ID]: messageResponse.id,
                  [SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS]: messageResponse.stop_reason
                    ? JSON.stringify([messageResponse.stop_reason]) : "[]",
                  // While an input hide setting is on only the response-side fields are recorded.
                  [SemanticConventions.OUTPUT_VALUE]: safelyJSONStringify(
                    privacy.hidesInput ? responseSide(messageResponse) : messageResponse) ?? "",
                  [SemanticConventions.OUTPUT_MIME_TYPE]: MimeType.JSON,
                  // The raw response is not recorded while any hide setting is on.
                  ...(privacy.hidesAny ? {} : { [SemanticConventions.RAW_OUTPUT]: safelyJSONStringify(messageResponse) ?? "" }),
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

    if (messagesPrototype.create === previousCreate) return moduleExports;
    patchedMessages.set(messagesPrototype, {
      owner: this,
      moduleExports,
      create: messagesPrototype.create,
      generation,
      hostedBy,
    });
    try {
      (moduleExports as any).fiPatched = true;
    } catch (e) {
      diag.warn(`Failed to set fiPatched flag on module '${MODULE_NAME}'. This is usually not an issue. Error: ${e}`);
    }
    return moduleExports;
  }

  private unpatch(moduleExports: typeof Anthropic & { fiPatched?: boolean }) {
    if (hostedClient(moduleExports)) {
      this.unpatchHosted(moduleExports);
      return;
    }
    const messagesPrototype = getMessagesPrototype(moduleExports);
    if (messagesPrototype) {
      this.unpatchMessages(messagesPrototype);
    } else {
      diag.warn("Anthropic.Messages.prototype not found during unpatch, could not unwrap messages.create.");
    }
  }

  private unpatchMessages(messagesPrototype: Anthropic.Messages) {
    const patch = patchedMessages.get(messagesPrototype);
    // Loader callbacks from a different instance must not remove our wrapper.
    if (!patch || patch.owner !== this) return;
    if (messagesPrototype.create === patch.create) this._unwrap(messagesPrototype, "create");
    patchedMessages.delete(messagesPrototype);
    try {
      (patch.moduleExports as any).fiPatched = false;
    } catch (e) {
      diag.warn(`Failed to unset fiPatched flag on module '${MODULE_NAME}'. This is usually not an issue. Error: ${e}`);
    }
  }
}
