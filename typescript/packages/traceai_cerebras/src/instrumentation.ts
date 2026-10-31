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
    delta?: { role?: string; content?: string | null };
    text?: string;
    finish_reason?: string | null;
  }>;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
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
        const body = args[0] as Request | undefined;
        // Leave invalid requests to the SDK rather than changing its error contract.
        if (!body || typeof body !== "object")
          return original.apply(this, args);
        const { messages, prompt: _prompt, ...parameters } = body;
        const config = generateTraceConfig(instrumentation._traceConfig);
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
              [S.INPUT_VALUE]: json(body),
              [S.INPUT_MIME_TYPE]: MimeType.JSON,
              [S.RAW_INPUT]: json(body),
              // Parameters can contain tool schemas or stop text; hide them with inputs.
              ...(!config.hideInputs
                ? { [S.LLM_INVOCATION_PARAMETERS]: json(parameters) }
                : {}),
              ...(messages ? { [S.LLM_INPUT_MESSAGES]: json(messages) } : {}),
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
                  config.hideOutputs,
                );
              }
              if (result)
                span.setAttributes(completionAttributes(result as Completion));
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
    ] as const)
      if (value != null) attrs[key] = value;
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
  const contents = new Map<number, string>();
  const chunks: Completion[] = [];
  let finished = false;
  const end = (error?: unknown, complete = false) => {
    if (finished) return;
    finished = true;
    const messages = Array.from(contents.values(), (content) => ({
      role: "assistant",
      content,
    }));
    span.setAttributes({
      [S.OUTPUT_VALUE]: contents.get(0) ?? "",
      [S.OUTPUT_MIME_TYPE]: MimeType.TEXT,
      [S.LLM_OUTPUT_MESSAGES]: json(messages),
      [S.RAW_OUTPUT]: json(chunks),
    });
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
          if (!hideOutputs) {
            chunks.push(next.value);
            for (const choice of getChoices(next.value)) {
              const content = choice.delta?.content ?? choice.text ?? "";
              contents.set(
                choice.index,
                (contents.get(choice.index) ?? "") + content,
              );
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

  // SDK tee() uses iterator(), whereas toReadableStream() uses asyncIterator.
  // Intercept the factory so all routes share one observation without replacing
  // the SDK Stream with an async generator that loses its controller/helpers.
  const iteratorKey =
    typeof stream.iterator === "function" ? "iterator" : Symbol.asyncIterator;
  return new Proxy(stream, {
    get(target, key, receiver) {
      const value = Reflect.get(target, key, target);
      if (key === iteratorKey) return () => iterate(value.call(target));
      return typeof value === "function" && key !== "constructor"
        ? value.bind(receiver)
        : value;
    },
  });
}
