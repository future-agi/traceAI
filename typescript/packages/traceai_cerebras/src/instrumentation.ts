import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
} from "@opentelemetry/instrumentation";
import {
  context,
  trace,
  SpanKind,
  Attributes,
  SpanStatusCode,
  Span,
  Context,
} from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import {
  FITracer,
  safelyJSONStringify,
  TraceConfigOptions,
  generateTraceConfig,
} from "@traceai/fi-core";
import {
  SemanticConventions as S,
  FISpanKind,
  MimeType,
  LLMProvider,
} from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

const MODULE_NAME = "@cerebras/cerebras_cloud_sdk";
const json = (value: unknown) => safelyJSONStringify(value) ?? undefined;
type Operation = "chat" | "text_completion";
type SDKModule = Record<string, unknown>;
interface Request {
  model?: string;
  messages?: Array<Record<string, unknown>>;
  prompt?: unknown;
  [key: string]: unknown;
}
interface Completion {
  id?: string;
  model?: string;
  choices?: Array<{
    index: number;
    message?: Record<string, unknown>;
    delta?: {
      role?: string;
      content?: string | null;
      reasoning?: string | null;
      tool_calls?: Array<{
        index: number;
        id?: string;
        type?: string;
        function?: { name?: string; arguments?: string };
      }>;
    };
    text?: string;
    finish_reason?: string | null;
  }>;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    prompt_tokens_details?: { cached_tokens?: number } | null;
    completion_tokens_details?: { reasoning_tokens?: number } | null;
  };
  time_info?: Record<string, number>;
}
interface Stream extends AsyncIterable<Completion> {
  iterator?: () => AsyncIterator<Completion>;
  controller?: AbortController;
}
type Result = Completion | Stream;
type Create = (this: unknown, ...args: unknown[]) => Promise<Result>;
type Resource = { create: Create };
interface SDKClass {
  Chat?: { Completions?: { prototype: Resource } };
  Completions?: { prototype: Resource };
}

// Ownership is per resource, not per module name: separate SDK copies can coexist.
const owners = new WeakMap<Resource, CerebrasInstrumentation>();
let patchedResources = 0;
export function isPatched() {
  return patchedResources > 0;
}

function resources(
  module: SDKModule,
): Array<[Resource | undefined, Operation]> {
  // CJS exports the constructor itself; ESM exposes named/default constructors.
  const sdk = (module.Cerebras ?? module.default ?? module) as SDKClass;
  return [
    [sdk.Chat?.Completions?.prototype, "chat"],
    [sdk.Completions?.prototype, "text_completion"],
  ];
}

// Inspect descriptors, never values obtained through getters. Both telemetry
// serialization and the SDK traverse nested messages/tools, so a shallow check
// would still evaluate those accessors twice. Repeated references are harmless.
function hasOpaqueRequestValue(body: object): boolean {
  const pending = [body];
  const seen = new WeakSet<object>();
  while (pending.length) {
    const value = pending.pop()!;
    if (seen.has(value)) continue;
    seen.add(value);
    if ("toJSON" in value) return true;
    for (
      let holder: object | null = value;
      holder && holder !== Object.prototype && holder !== Array.prototype;
      holder = Object.getPrototypeOf(holder)
    ) {
      for (const key of Reflect.ownKeys(holder)) {
        const descriptor = Object.getOwnPropertyDescriptor(holder, key)!;
        if (!("value" in descriptor)) return true;
        if (
          holder === value && descriptor.enumerable && descriptor.value &&
          (typeof descriptor.value === "object" || typeof descriptor.value === "function")
        ) pending.push(descriptor.value);
      }
    }
  }
  return false;
}

export class CerebrasInstrumentation extends InstrumentationBase {
  private _traceConfig?: TraceConfigOptions;
  private manualModules?: Set<SDKModule>;
  private patched?: Set<Resource>;
  public readonly instrumentationName = "@traceai/cerebras";

  constructor({
    instrumentationConfig,
    traceConfig,
  }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/cerebras", VERSION, instrumentationConfig ?? {});
    this._traceConfig = traceConfig;
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition(
      MODULE_NAME,
      [">=1.0.0"],
      this.patch.bind(this),
      this.unpatch.bind(this),
    );
  }

  public override enable(): void {
    super.enable();
    this.manualModules?.forEach((module) => this.patch(module));
  }

  public override disable(): void {
    super.disable();
    // Manual instrumentation also needs cleanup, including pre-imported ESM.
    this.patched?.forEach((resource) => this.unpatchResource(resource));
  }

  manuallyInstrument(module: SDKModule) {
    (this.manualModules ??= new Set()).add(module);
    this.patch(module);
  }

  private patch(module: SDKModule) {
    for (const [resource, operation] of resources(module)) {
      if (
        !resource ||
        typeof resource.create !== "function" ||
        owners.has(resource)
      )
        continue;
      this._wrap(resource, "create", this.createCompletionWrapper(operation));
      owners.set(resource, this);
      (this.patched ??= new Set()).add(resource);
      patchedResources++;
    }
    return module;
  }

  private unpatch(module: SDKModule) {
    for (const [resource] of resources(module))
      if (resource) this.unpatchResource(resource);
  }

  private unpatchResource(resource: Resource) {
    if (owners.get(resource) !== this) return;
    this._unwrap(resource, "create");
    owners.delete(resource);
    this.patched?.delete(resource);
    patchedResources--;
  }

  private createCompletionWrapper(operation: Operation) {
    const instrumentation = this;
    return (original: Create): Create =>
      function (...args: unknown[]) {
        if (
          !instrumentation.isEnabled() ||
          isTracingSuppressed(context.active())
        )
          return original.apply(this, args);
        // The SDK spreads RequestOptions over its body. An own enumerable body
        // replaces the positional payload, even when explicitly null/undefined.
        // Inspect without reading accessors: the SDK must evaluate them exactly
        // once. Headers and other transport options are not trace data.
        const options = args[1];
        const bodyDescriptor = options == null
          ? undefined
          : Object.getOwnPropertyDescriptor(options, "body");
        if (bodyDescriptor?.enumerable && !("value" in bodyDescriptor))
          return original.apply(this, args);
        const body = (
          bodyDescriptor?.enumerable ? bodyDescriptor.value : args[0]
        ) as Request | undefined;
        // Leave non-request bodies to the SDK; never fall back to unsent input.
        if (!body || typeof body !== "object" || Array.isArray(body))
          return original.apply(this, args);
        // Accessors and serializers may be stateful or omit private fields.
        // Let only the SDK evaluate opaque bodies, before starting any span.
        if (hasOpaqueRequestValue(body)) return original.apply(this, args);
        const { messages, prompt: _prompt, ...parameters } = body;
        const config = generateTraceConfig(instrumentation._traceConfig);
        // Raw payloads duplicate canonical messages. Omit every representation
        // when a content control is active, before serializing or buffering it.
        const hideInputPayload =
          config.hideInputs ||
          config.hideInputMessages ||
          config.hideInputText ||
          config.hideInputImages;
        const hideOutputPayload =
          config.hideOutputs ||
          config.hideOutputMessages ||
          config.hideOutputText;
        const tracer = new FITracer({
          tracer: instrumentation.tracer,
          traceConfig: config,
        });
        const span = tracer.startSpan(
          operation === "chat"
            ? "Cerebras Chat Completions"
            : "Cerebras Completions",
          {
            kind: SpanKind.INTERNAL,
            attributes: {
              [S.FI_SPAN_KIND]: FISpanKind.LLM,
              [S.LLM_MODEL_NAME]: body.model,
              [S.LLM_PROVIDER]: LLMProvider.CEREBRAS,
              [S.GEN_AI_OPERATION_NAME]: operation,
              ...(!hideInputPayload
                ? {
                    [S.INPUT_VALUE]: json(body),
                    [S.INPUT_MIME_TYPE]: MimeType.JSON,
                    [S.RAW_INPUT]: json(body),
                    // Parameters can contain tool schemas or stop text.
                    [S.LLM_INVOCATION_PARAMETERS]: json(parameters),
                    ...(messages
                      ? { [S.LLM_INPUT_MESSAGES]: json(messages) }
                      : {}),
                  }
                : {}),
            },
          },
        );
        const execContext = trace.setSpan(context.active(), span);
        let ended = false;
        const finish = (error?: unknown, complete = true) => {
          if (ended) return;
          ended = true;
          if (error !== undefined) {
            const exception =
              error instanceof Error ? error : new Error(String(error));
            span.recordException(exception);
            span.setStatus({
              code: SpanStatusCode.ERROR,
              message: exception.message,
            });
          } else if (complete) span.setStatus({ code: SpanStatusCode.OK });
          span.end();
        };
        try {
          const promise = context.with(execContext, () =>
            original.apply(this, args),
          );
          return observePromise(
            promise,
            (result) => {
              if (
                result &&
                typeof result === "object" &&
                Symbol.asyncIterator in result
              ) {
                return observeStream(
                  result as Stream,
                  span,
                  execContext,
                  finish,
                  hideOutputPayload,
                );
              }
              if (result)
                span.setAttributes(
                  hideOutputPayload
                    ? metadataAttributes(result as Completion)
                    : completionAttributes(result as Completion),
                );
              finish();
              return result;
            },
            finish,
            execContext,
          );
        } catch (error) {
          finish(error);
          throw error;
        }
      };
  }
}

/** Observe only the consumption route chosen by the caller. In particular,
 * attaching .then eagerly would steal the body from APIPromise.asResponse(). */
function observePromise<T>(
  promise: Promise<T>,
  observe: (value: T) => T,
  finish: (error?: unknown) => void,
  execContext: Context,
): Promise<T> {
  let observed: Promise<T> | undefined;
  let valueObserved = false;
  let value: T;
  const onValue = (result: T) => {
    if (!valueObserved) {
      value = observe(result);
      valueObserved = true;
    }
    return value;
  };
  const onError = (error: unknown): never => {
    finish(error);
    throw error;
  };
  const parsed = () =>
    (observed ??= context.with(execContext, () =>
      promise.then(onValue, onError),
    ));
  return new Proxy(promise, {
    get(target, key) {
      if (key === "then")
        return (...args: Parameters<Promise<T>["then"]>) =>
          parsed().then(...args);
      if (key === "catch")
        return (...args: Parameters<Promise<T>["catch"]>) =>
          parsed().catch(...args);
      if (key === "finally")
        return (...args: Parameters<Promise<T>["finally"]>) =>
          parsed().finally(...args);
      const method = Reflect.get(target, key, target);
      if (key === "withResponse" && typeof method === "function") {
        return (...args: unknown[]) =>
          context
            .with(execContext, () => method.apply(target, args))
            .then(
              (result: { data: T }) => ({
                ...result,
                data: onValue(result.data),
              }),
              onError,
            );
      }
      if (key === "asResponse" && typeof method === "function") {
        return (...args: unknown[]) =>
          context
            .with(execContext, () => method.apply(target, args))
            .then((response: unknown) => {
              finish();
              return response;
            }, onError);
      }
      return typeof method === "function" && key !== "constructor"
        ? method.bind(target)
        : method;
    },
  });
}

function getChoices(response: Completion) {
  return Array.isArray(response.choices)
    ? response.choices.filter((choice) => choice && typeof choice === "object")
    : [];
}

function completionAttributes(response: Completion): Attributes {
  const attrs: Attributes = {
    [S.OUTPUT_VALUE]: json(response),
    [S.OUTPUT_MIME_TYPE]: MimeType.JSON,
    [S.RAW_OUTPUT]: json(response),
    ...metadataAttributes(response),
  };
  const messages = getChoices(response).map(
    (choice) => choice.message ?? { role: "assistant", content: choice.text },
  );
  if (messages) attrs[S.LLM_OUTPUT_MESSAGES] = json(messages);
  return attrs;
}

function metadataAttributes(response: Completion): Attributes {
  const attrs: Attributes = {};
  if (response.model) {
    attrs[S.GEN_AI_RESPONSE_MODEL] = response.model;
  }
  if (response.id) attrs[S.GEN_AI_RESPONSE_ID] = response.id;
  const reasons = getChoices(response)
    .map((c) => c.finish_reason)
    .filter((reason) => reason != null);
  if (reasons?.length) attrs[S.GEN_AI_RESPONSE_FINISH_REASONS] = json(reasons);
  if (response.usage) {
    for (const [key, value] of [
      [S.LLM_TOKEN_COUNT_PROMPT, response.usage.prompt_tokens],
      [S.LLM_TOKEN_COUNT_COMPLETION, response.usage.completion_tokens],
      [S.LLM_TOKEN_COUNT_TOTAL, response.usage.total_tokens],
      [
        S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ,
        response.usage.prompt_tokens_details?.cached_tokens,
      ],
      [
        S.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING,
        response.usage.completion_tokens_details?.reasoning_tokens,
      ],
    ] as const)
      if (typeof value === "number" && Number.isFinite(value)) attrs[key] = value;
  }
  for (const key of [
    "queue_time",
    "prompt_time",
    "completion_time",
    "total_time",
  ]) {
    const value = response.time_info?.[key];
    if (value != null) attrs[`cerebras.${key}`] = value;
  }
  return attrs;
}

function observeStream(
  stream: Stream,
  span: Span,
  execContext: Context,
  finish: (error?: unknown, complete?: boolean) => void,
  hideOutputs: boolean,
): Stream {
  const choices = new Map<
    number,
    {
      role: string;
      content: string;
      reasoning?: string;
      toolCalls: Map<
        number,
        {
          id?: string;
          type?: string;
          function: { name: string; arguments: string };
        }
      >;
    }
  >();
  const finishReasons = new Map<number, string>();
  const chunks: Completion[] = [];
  let finished = false;
  const end = (error?: unknown, complete = false) => {
    if (finished) return;
    finished = true;
    if (finishReasons.size) {
      span.setAttribute(
        S.GEN_AI_RESPONSE_FINISH_REASONS,
        json(
          Array.from(finishReasons)
            .sort(([a], [b]) => a - b)
            .map(([, reason]) => reason),
        )!,
      );
    }
    if (!hideOutputs) {
      const messages = Array.from(choices)
        .sort(([a], [b]) => a - b)
        .map(([, choice]) => ({
          role: choice.role,
          content: choice.content,
          ...(choice.reasoning !== undefined
            ? { reasoning: choice.reasoning }
            : {}),
          ...(choice.toolCalls.size
            ? {
                tool_calls: Array.from(choice.toolCalls)
                  .sort(([a], [b]) => a - b)
                  .map(([, call]) => call),
              }
            : {}),
        }));
      const hasTools = messages.some((message) => message.tool_calls?.length);
      span.setAttributes({
        [S.OUTPUT_VALUE]: hasTools
          ? json(messages)
          : (choices.get(0)?.content ?? ""),
        [S.OUTPUT_MIME_TYPE]: hasTools ? MimeType.JSON : MimeType.TEXT,
        [S.LLM_OUTPUT_MESSAGES]: json(messages),
        [S.RAW_OUTPUT]: json(chunks),
      });
    }
    stream.controller?.signal.removeEventListener("abort", aborted);
    finish(error, complete);
  };
  const aborted = () => {
    // The SDK also aborts while unwinding stream errors. Let the iterator's
    // catch retain that error rather than turning it into a successful span.
    if (!iterating) end();
  };
  let iterating = false;
  stream.controller?.signal.addEventListener("abort", aborted, { once: true });
  if (stream.controller?.signal.aborted) end();

  async function* iterate(iterator: AsyncIterator<Completion>) {
    try {
      while (true) {
        iterating = true;
        const next = await context.with(execContext, () => iterator.next());
        iterating = false;
        if (next.done) {
          end(undefined, !stream.controller?.signal.aborted);
          return;
        }
        if (!finished && next.value && typeof next.value === "object") {
          span.setAttributes(metadataAttributes(next.value));
          if (!hideOutputs) chunks.push(next.value);
          for (const choice of getChoices(next.value)) {
            if (choice.finish_reason != null)
              finishReasons.set(choice.index, choice.finish_reason);
            if (hideOutputs) continue;
            let accumulated = choices.get(choice.index);
            if (!accumulated) {
              accumulated = {
                role: "assistant",
                content: "",
                toolCalls: new Map(),
              };
              choices.set(choice.index, accumulated);
            }
            if (choice.delta?.role) accumulated.role = choice.delta.role;
            accumulated.content += choice.delta?.content ?? choice.text ?? "";
            if (typeof choice.delta?.reasoning === "string")
              accumulated.reasoning =
                (accumulated.reasoning ?? "") + choice.delta.reasoning;
            for (const delta of choice.delta?.tool_calls ?? []) {
              let call = accumulated.toolCalls.get(delta.index);
              if (!call) {
                call = { function: { name: "", arguments: "" } };
                accumulated.toolCalls.set(delta.index, call);
              }
              if (delta.id) call.id = delta.id;
              if (delta.type) call.type = delta.type;
              call.function.name += delta.function?.name ?? "";
              call.function.arguments += delta.function?.arguments ?? "";
            }
          }
        }
        yield next.value;
      }
    } catch (error) {
      end(error);
      throw error;
    } finally {
      try {
        await context.with(execContext, () => iterator.return?.());
      } finally {
        iterating = false;
        end();
      }
    }
  }

  function observeIterator(
    iterator: AsyncIterator<Completion>,
  ): AsyncIterableIterator<Completion> {
    const observed = iterate(iterator);
    let started = false;
    return {
      [Symbol.asyncIterator]() {
        return this;
      },
      next() {
        started = true;
        return observed.next();
      },
      async return(value) {
        try {
          // Returning an unstarted generator never enters its finally block.
          if (!started) {
            started = true;
            await context.with(execContext, () => iterator.return?.(value));
          }
          return await observed.return(value);
        } catch (error) {
          end(error);
          throw error;
        } finally {
          end();
        }
      },
      async throw(error) {
        try {
          if (!started) {
            started = true;
            await context.with(execContext, () => iterator.return?.());
          }
          return await observed.throw(error);
        } catch (failure) {
          end(failure);
          throw failure;
        } finally {
          end();
        }
      },
    };
  }

  // SDK tee() uses iterator(), whereas toReadableStream() uses asyncIterator.
  // Intercept the factory so all routes share one observation without replacing
  // the SDK Stream with an async generator that loses its controller/helpers.
  const iteratorKey =
    typeof stream.iterator === "function" ? "iterator" : Symbol.asyncIterator;
  return new Proxy(stream, {
    get(target, key, receiver) {
      const value = Reflect.get(target, key, target);
      if (key === iteratorKey) return () => observeIterator(value.call(target));
      return typeof value === "function" && key !== "constructor"
        ? value.bind(receiver)
        : value;
    },
  });
}
