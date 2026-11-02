import {
  InstrumentationBase,
  InstrumentationConfig,
  InstrumentationNodeModuleDefinition,
  isWrapped,
} from "@opentelemetry/instrumentation";
import { diag, context, trace, SpanKind, Attributes, SpanStatusCode, Span } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { FITracer, safelyJSONStringify, TraceConfigOptions, generateTraceConfig } from "@traceai/fi-core";
import { SemanticConventions as S, FISpanKind, MimeType, LLMProvider } from "@traceai/fi-semantic-conventions";
import { VERSION } from "./version";

interface ToolCall {
  index?: number;
  id?: string;
  type?: string;
  function?: { name?: string; arguments?: string };
}
interface ChatMessage {
  role?: string;
  content?: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  reasoning_content?: string;
}
interface ChatCompletionRequest {
  model: string;
  messages: ChatMessage[];
  stream?: boolean;
  tools?: unknown[];
  [key: string]: unknown;
}
interface ChatCompletion {
  id?: string;
  model?: string;
  choices: {
    index: number;
    message?: ChatMessage;
    delta?: ChatMessage;
    finish_reason?: string | null;
  }[];
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    prompt_cache_hit_tokens?: number;
    prompt_cache_miss_tokens?: number;
  };
}
interface StreamResponse extends AsyncIterable<ChatCompletion> {
  iterator?: () => AsyncIterator<ChatCompletion>;
  controller?: AbortController;
}
// These are runtime contracts, not imports of one particular OpenAI SDK major.
type Create = (this: unknown, ...args: unknown[]) => unknown;
interface Resource { create: Create }
interface ModuleExports {
  OpenAI?: { Chat?: { Completions?: { prototype: Resource } } };
  default?: ModuleExports["OpenAI"];
  Chat?: { Completions?: { prototype: Resource } };
}
interface Patch { resource: Resource; wrapped: Create }
const patches = new Set<Patch>();

/** Whether a DeepSeek resource wrapper is currently installed. */
export function isPatched(): boolean {
  return [...patches].some(({ resource, wrapped }) => resource.create === wrapped);
}

/** Instruments DeepSeek Chat Completions through the official OpenAI SDK. */
export class DeepSeekInstrumentation extends InstrumentationBase {
  private _traceConfig?: TraceConfigOptions;
  private ownedPatches?: Set<Patch>;
  private manualModules?: Set<ModuleExports>;
  public readonly instrumentationName = "@traceai/deepseek";

  constructor({ instrumentationConfig, traceConfig }: {
    instrumentationConfig?: InstrumentationConfig;
    traceConfig?: TraceConfigOptions;
  } = {}) {
    super("@traceai/deepseek", VERSION, instrumentationConfig ?? {});
    this._traceConfig = traceConfig;
  }

  protected init() {
    return new InstrumentationNodeModuleDefinition("openai", [">=4.0.0"],
      (module) => this.patch(module), (module) => this.unpatch(module));
  }

  public override enable(): void {
    super.enable();
    // The base constructor invokes enable before subclass fields are initialized.
    for (const module of this.manualModules ?? []) this.patch(module);
  }

  public override disable(): void {
    super.disable();
    for (const patch of this.ownedPatches ?? []) this.removePatch(patch);
  }

  /** Use this for modules loaded before registration or through ESM. */
  manuallyInstrument(module: Record<string, unknown>) {
    (this.manualModules ??= new Set()).add(module);
    this.patch(module);
  }

  private isDeepSeekRequest(resource: unknown): boolean {
    const value = resource as { _client?: { baseURL?: unknown }; client?: { baseURL?: unknown }; baseURL?: unknown };
    const baseURL = (value?._client ?? value?.client ?? value)?.baseURL;
    if (typeof baseURL !== "string") return false;
    try {
      const url = new URL(baseURL);
      return (url.protocol === "https:" || url.protocol === "http:") && url.hostname === "api.deepseek.com";
    } catch { return false; }
  }

  private resource(module: ModuleExports): Resource | undefined {
    return (module.OpenAI ?? module.default ?? module).Chat?.Completions?.prototype;
  }

  private patch(module: ModuleExports) {
    const resource = this.resource(module);
    if (!resource || typeof resource.create !== "function") return module;
    if ([...patches].some(p => p.resource === resource && p.wrapped === resource.create)) return module;
    // Do not replace another instrumentor's wrapper. Register one OpenAI-compatible
    // instrumentor for a given SDK module, not both the shared and dedicated routes.
    if (isWrapped(resource.create)) {
      diag.warn("DeepSeek: chat.completions.create is already instrumented; register only one OpenAI-compatible instrumentor.");
      return module;
    }
    this._wrap(resource, "create", this.createChatCompletionWrapper(this));
    const patch = { resource, wrapped: resource.create };
    patches.add(patch);
    (this.ownedPatches ??= new Set()).add(patch);
    return module;
  }

  private removePatch(patch: Patch) {
    if (patch.resource.create === patch.wrapped) this._unwrap(patch.resource, "create");
    patches.delete(patch);
    this.ownedPatches?.delete(patch);
  }

  private unpatch(module: ModuleExports) {
    const resource = this.resource(module);
    for (const patch of this.ownedPatches ?? []) {
      if (patch.resource === resource) this.removePatch(patch);
    }
  }

  private createChatCompletionWrapper(instrumentation: DeepSeekInstrumentation) {
    return (original: Create): Create => function (this: unknown, ...args: unknown[]) {
      if (!instrumentation.isDeepSeekRequest(this) || isTracingSuppressed(context.active())) {
        return original.apply(this, args);
      }
      const body = args[0] as ChatCompletionRequest;
      // Resolve privacy only after construction, and use the latest tracer provider.
      const config = generateTraceConfig(instrumentation._traceConfig);
      const hideInput = config.hideInputs || config.hideInputMessages || config.hideInputText || config.hideInputImages;
      const hideOutput = config.hideOutputs || config.hideOutputMessages || config.hideOutputText;
      const tracer = new FITracer({ tracer: instrumentation.tracer, traceConfig: config });
      const span = tracer.startSpan("DeepSeek Chat Completions", {
        kind: SpanKind.INTERNAL,
        attributes: {
          [S.FI_SPAN_KIND]: FISpanKind.LLM,
          [S.LLM_MODEL_NAME]: body?.model,
          [S.LLM_PROVIDER]: LLMProvider.DEEPSEEK,
          [S.GEN_AI_OPERATION_NAME]: "chat",
          ...(!hideInput ? inputAttributes(body) : {}),
        },
      });
      const execContext = trace.setSpan(context.active(), span);
      let ended = false;
      const finish = (error?: unknown) => {
        if (ended) return;
        ended = true;
        if (error !== undefined) {
          span.recordException(error instanceof Error ? error : new Error(String(error)));
          span.setStatus({ code: SpanStatusCode.ERROR, message: error instanceof Error ? error.message : String(error) });
        } else {
          span.setStatus({ code: SpanStatusCode.OK });
        }
        span.end();
      };
      let observedResult = false;
      const observe = (result: unknown) => {
        if (observedResult) return result;
        observedResult = true;
        if (result && typeof (result as StreamResponse)[Symbol.asyncIterator] === "function") {
          observeStream(result as StreamResponse, span, hideOutput, finish, execContext);
        } else {
          if (result && Array.isArray((result as ChatCompletion).choices)) {
            span.setAttributes(outputAttributes(result as ChatCompletion, hideOutput));
          }
          finish();
        }
        return result;
      };
      try {
        const result = context.with(execContext, () => original.apply(this, args));
        return observePromise(result, observe, finish);
      } catch (error) {
        finish(error);
        throw error;
      }
    };
  }
}

/** Preserve APIPromise identity, helpers and lazy parsing (including asResponse). */
function observePromise(result: unknown, observe: (value: unknown) => unknown, finish: (error?: unknown) => void): unknown {
  const promise = result as {
    parse?: () => Promise<unknown>;
    asResponse?: () => Promise<unknown>;
    _thenUnwrap?: (...args: unknown[]) => unknown;
    then: Promise<unknown>["then"];
  };
  const onError = (error: unknown): never => { finish(error); throw error; };
  if (typeof promise.parse === "function") {
    const parse = promise.parse.bind(promise);
    let observed: Promise<unknown> | undefined;
    promise.parse = () => observed ??= parse().then(observe, onError);
    // SDK helpers such as chat.completions.parse derive another APIPromise without
    // invoking this promise's parse method. Observe that derived promise too.
    if (typeof promise._thenUnwrap === "function") {
      const unwrap = promise._thenUnwrap.bind(promise);
      promise._thenUnwrap = (...args) => observePromise(unwrap(...args), observe, finish);
    }
    if (typeof promise.asResponse === "function") {
      const asResponse = promise.asResponse.bind(promise);
      promise.asResponse = () => asResponse().then(response => {
        // withResponse calls parse first; raw-only callers keep ownership of the body.
        if (!observed) finish();
        return response;
      }, onError);
    }
    return result;
  }
  // Also support ordinary promises in compatible clients and unit tests.
  return promise.then(observe, onError);
}

function json(value: unknown): string | undefined {
  return safelyJSONStringify(value) ?? undefined;
}

function inputAttributes(body: ChatCompletionRequest): Attributes {
  const { messages, ...parameters } = body;
  return {
    [S.INPUT_VALUE]: json(body),
    [S.INPUT_MIME_TYPE]: MimeType.JSON,
    [S.RAW_INPUT]: json(body),
    [S.LLM_INPUT_MESSAGES]: json(messages),
    [S.LLM_INVOCATION_PARAMETERS]: json(parameters),
    ...(body.tools ? { [S.LLM_TOOLS]: json(body.tools) } : {}),
  };
}

function usageAttributes(response: ChatCompletion): Attributes {
  const usage = response.usage;
  return usage ? {
    [S.LLM_TOKEN_COUNT_PROMPT]: usage.prompt_tokens,
    [S.LLM_TOKEN_COUNT_COMPLETION]: usage.completion_tokens,
    [S.LLM_TOKEN_COUNT_TOTAL]: usage.total_tokens,
    "deepseek.prompt_cache_hit_tokens": usage.prompt_cache_hit_tokens,
    "deepseek.prompt_cache_miss_tokens": usage.prompt_cache_miss_tokens,
  } : {};
}

function outputAttributes(response: ChatCompletion, hideOutput: boolean): Attributes {
  return {
    [S.GEN_AI_RESPONSE_MODEL]: response.model,
    [S.GEN_AI_RESPONSE_ID]: response.id,
    [S.GEN_AI_RESPONSE_FINISH_REASONS]: json(response.choices.map(c => c.finish_reason).filter(Boolean)),
    ...usageAttributes(response),
    ...(!hideOutput ? {
      [S.OUTPUT_VALUE]: json(response),
      [S.OUTPUT_MIME_TYPE]: MimeType.JSON,
      [S.RAW_OUTPUT]: json(response),
      [S.LLM_OUTPUT_MESSAGES]: json(response.choices.map(c => c.message)),
      "deepseek.reasoning_content": response.choices[0]?.message?.reasoning_content,
    } : {}),
  };
}

/** Observe only the caller's consumption; never tee or drain the stream in the background. */
function observeStream(stream: StreamResponse, span: Span, hideOutput: boolean,
  finish: (error?: unknown) => void, execContext: ReturnType<typeof context.active>) {
  const response: ChatCompletion = { choices: [] };
  const choices = new Map<number, NonNullable<ChatCompletion["choices"][number]["message"]>>();
  const tools = new Map<number, Map<number, ToolCall>>();
  const chunks: ChatCompletion[] = [];
  let completed = false;
  const complete = (error?: unknown) => {
    if (completed) return;
    completed = true;
    stream.controller?.signal.removeEventListener("abort", aborted);
    span.setAttributes(outputAttributes(response, hideOutput));
    if (!hideOutput) span.setAttributes({
      [S.OUTPUT_VALUE]: response.choices[0]?.message?.content ?? "",
      [S.OUTPUT_MIME_TYPE]: MimeType.TEXT,
      [S.RAW_OUTPUT]: json(chunks),
    });
    finish(error);
  };
  // The SDK also aborts internally while propagating iteration failures. Let the
  // pending next/return report that failure instead of ending an OK span first.
  let pendingNext = false;
  const aborted = () => { if (!pendingNext) complete(); };
  stream.controller?.signal.addEventListener("abort", aborted, { once: true });
  if (stream.controller?.signal.aborted) complete();

  const collect = (chunk: ChatCompletion) => {
    if (chunk.id) response.id = chunk.id;
    if (chunk.model) response.model = chunk.model;
    if (chunk.usage) response.usage = chunk.usage;
    if (!hideOutput) chunks.push(chunk);
    for (const choice of chunk.choices ?? []) {
      let target = response.choices.find(c => c.index === choice.index);
      if (!target) {
        target = { index: choice.index, message: { role: "assistant", content: "" } };
        response.choices.push(target);
        choices.set(choice.index, target.message!);
      }
      if (choice.finish_reason) target.finish_reason = choice.finish_reason;
      if (hideOutput || !choice.delta) continue;
      const message = choices.get(choice.index)!;
      message.content = (message.content ?? "") + (choice.delta.content ?? "");
      message.reasoning_content = (message.reasoning_content ?? "") + (choice.delta.reasoning_content ?? "");
      for (const part of choice.delta.tool_calls ?? []) {
        const calls = tools.get(choice.index) ?? new Map<number, ToolCall>();
        tools.set(choice.index, calls);
        const index = part.index ?? 0;
        const call = calls.get(index) ?? { type: "function", function: { name: "", arguments: "" } };
        if (part.id) call.id = part.id;
        if (part.type) call.type = part.type;
        call.function!.name += part.function?.name ?? "";
        call.function!.arguments += part.function?.arguments ?? "";
        calls.set(index, call);
        message.tool_calls = [...calls.entries()].sort(([a], [b]) => a - b).map(([, value]) => value);
      }
    }
  };
  // Modern Stream helpers call the iterator factory directly; the 4.0 floor only
  // has Symbol.asyncIterator. Keep the original Stream/controller/helper methods.
  const publicFactory = stream[Symbol.asyncIterator].bind(stream);
  const factory = (stream.iterator ?? stream[Symbol.asyncIterator]).bind(stream);
  const observed = (iterator: AsyncIterator<ChatCompletion>, capture: boolean): AsyncIterableIterator<ChatCompletion> => {
    const invoke = async (operation: () => Promise<IteratorResult<ChatCompletion>>, closing: boolean) => {
      pendingNext = true;
      try {
        const next = await context.with(execContext, operation);
        if (closing || next.done) complete();
        else if (capture) collect(next.value);
        return next;
      } catch (error) {
        complete(error);
        throw error;
      } finally { pendingNext = false; }
    };
    return {
      [Symbol.asyncIterator]() { return this; },
      next: () => invoke(() => iterator.next(), false),
      // An async generator's finally never runs if return() precedes its first
      // next(). Explicit forwarding also closes the span for that valid exit.
      return: value => invoke(() => iterator.return ? iterator.return(value) : Promise.resolve({ done: true, value }), true),
      throw: error => invoke(async () => {
        if (iterator.throw) return iterator.throw(error);
        if (iterator.return) await iterator.return();
        throw error;
      }, true),
    };
  };
  if (typeof stream.iterator === "function") {
    stream.iterator = () => observed(factory(), true);
    // Some SDK releases decorate the public iterator with a lazy normalizer.
    // Preserve it, but observe its return-before-next path as well. Chunks are
    // collected only at the underlying factory, including tee/readable helpers.
    stream[Symbol.asyncIterator] = () => observed(publicFactory(), false);
  } else stream[Symbol.asyncIterator] = () => observed(factory(), true);
}
