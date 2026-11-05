import { Attributes, Context, Span, SpanStatusCode, context } from "@opentelemetry/api";
import { SemanticConventions as S, MimeType } from "@traceai/fi-semantic-conventions";
import { safelyJSONStringify } from "@traceai/fi-core";

interface Usage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  prompt_tokens_details?: { cached_tokens?: number } | null;
  completion_tokens_details?: { reasoning_tokens?: number } | null;
  dram_cached_tokens?: number;
  sram_cached_tokens?: number;
}
interface ToolCall {
  index?: number;
  id?: string;
  type?: string;
  function?: { name?: string; arguments?: string };
}
interface Message {
  role?: string;
  content?: string | null;
  reasoning?: string | null;
  tool_calls?: ToolCall[];
}
interface Choice { index: number; message?: Message; delta?: Message; finish_reason?: string | null }
interface Completion {
  id?: string;
  model?: string;
  choices: Choice[];
  usage?: Usage | null;
  x_groq?: { usage?: Usage | null; error?: string | null };
}
interface Stream extends AsyncIterable<Completion> {
  controller: AbortController;
}
type Result = Completion | Stream;
interface ResponsePromise<T> extends Promise<T> {
  asResponse?(): Promise<unknown>;
  withResponse?(): Promise<{ data: T; [key: string]: unknown }>;
}

/** Audio returns either JSON (including verbose segments) or a plain text transcript. */
export function audioResponseAttributes(result: unknown): Attributes {
  const text = typeof result === "string";
  const output = text ? result : safelyJSONStringify(result) ?? undefined;
  return {
    [S.OUTPUT_VALUE]: output,
    [S.OUTPUT_MIME_TYPE]: text ? MimeType.TEXT : MimeType.JSON,
    [S.RAW_OUTPUT]: output,
  };
}

/** Usage values are snapshots, not deltas. Prefer present newer fields, including zero. */
function mergeUsage(previous?: Usage | null, next?: Usage | null): Usage | undefined {
  if (!next) return previous ?? undefined;
  if (!previous) return next;
  return {
    ...previous, ...next,
    prompt_tokens: next.prompt_tokens ?? previous.prompt_tokens,
    completion_tokens: next.completion_tokens ?? previous.completion_tokens,
    total_tokens: next.total_tokens ?? previous.total_tokens,
    prompt_tokens_details: {
      ...previous.prompt_tokens_details, ...next.prompt_tokens_details,
      cached_tokens: next.prompt_tokens_details?.cached_tokens ?? previous.prompt_tokens_details?.cached_tokens,
    },
    completion_tokens_details: {
      ...previous.completion_tokens_details, ...next.completion_tokens_details,
      reasoning_tokens: next.completion_tokens_details?.reasoning_tokens ?? previous.completion_tokens_details?.reasoning_tokens,
    },
    dram_cached_tokens: next.dram_cached_tokens ?? previous.dram_cached_tokens,
    sram_cached_tokens: next.sram_cached_tokens ?? previous.sram_cached_tokens,
  };
}

function responseUsage(result: Completion): Usage | undefined {
  // New SDKs split standard usage and hardware cache metrics; older streams
  // put all usage in x_groq. Neither source should hide fields in the other.
  return mergeUsage(result.x_groq?.usage, result.usage);
}

function usageAttributes(usage?: Usage): Attributes {
  return {
    [S.LLM_TOKEN_COUNT_PROMPT]: usage?.prompt_tokens,
    [S.LLM_TOKEN_COUNT_COMPLETION]: usage?.completion_tokens,
    [S.LLM_TOKEN_COUNT_TOTAL]: usage?.total_tokens,
    [S.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ]: usage?.prompt_tokens_details?.cached_tokens,
    [S.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING]: usage?.completion_tokens_details?.reasoning_tokens,
    // Hardware cache counters are provider-specific, not additional billable tokens.
    "groq.usage.dram_cached_tokens": usage?.dram_cached_tokens,
    "groq.usage.sram_cached_tokens": usage?.sram_cached_tokens,
  };
}

function completionAttributes(result: Completion): Attributes {
  const choices = [...result.choices].sort((a, b) => a.index - b.index);
  return {
    [S.OUTPUT_VALUE]: safelyJSONStringify(result) ?? undefined,
    [S.OUTPUT_MIME_TYPE]: MimeType.JSON,
    [S.RAW_OUTPUT]: safelyJSONStringify(result) ?? undefined,
    [S.LLM_OUTPUT_MESSAGES]: safelyJSONStringify(choices.map(c => c.message)) ?? undefined,
    [S.GEN_AI_RESPONSE_MODEL]: result.model,
    [S.GEN_AI_RESPONSE_ID]: result.id,
    [S.GEN_AI_RESPONSE_FINISH_REASONS]: safelyJSONStringify(choices.map(c => c.finish_reason).filter(Boolean)) ?? undefined,
    ...usageAttributes(responseUsage(result)),
  };
}

/** Aggregate by SDK indexes, not by position within a delta. No chunk buffer or second reader. */
class Accumulator {
  private choices = new Map<number, { message: Message; tools: Map<number, ToolCall>; finish_reason?: string | null }>();
  private id?: string;
  private model?: string;
  private usage?: Usage;
  error?: string;

  add(chunk: Completion) {
    // Groq yields provider-stopped streams normally. Keep the diagnostic out of
    // output payloads and route it through FISpan's error-detail policy at end.
    if (typeof chunk.x_groq?.error === "string" && chunk.x_groq.error.length > 0) {
      this.error = chunk.x_groq.error;
    }
    this.id = chunk.id ?? this.id;
    this.model = chunk.model ?? this.model;
    this.usage = mergeUsage(this.usage, responseUsage(chunk));
    for (const choice of chunk.choices ?? []) {
      let acc = this.choices.get(choice.index);
      if (!acc) {
        acc = { message: { role: "assistant", content: "" }, tools: new Map() };
        this.choices.set(choice.index, acc);
      }
      if (choice.finish_reason != null) acc.finish_reason = choice.finish_reason;
      const delta = choice.delta;
      if (!delta) continue;
      if (delta.role) acc.message.role = delta.role;
      if (delta.content) acc.message.content += delta.content;
      if (delta.reasoning) acc.message.reasoning = (acc.message.reasoning ?? "") + delta.reasoning;
      for (const tool of delta.tool_calls ?? []) {
        // The SDK requires an index for streamed tools. Do not merge malformed
        // index-less deltas into an unrelated tool at array position zero.
        if (tool.index === undefined) continue;
        const previous = acc.tools.get(tool.index) ?? { function: { name: "", arguments: "" } };
        if (tool.id) previous.id = tool.id;
        if (tool.type) previous.type = tool.type;
        previous.function = {
          name: (previous.function?.name ?? "") + (tool.function?.name ?? ""),
          arguments: (previous.function?.arguments ?? "") + (tool.function?.arguments ?? ""),
        };
        acc.tools.set(tool.index, previous);
      }
    }
  }

  attributes(): Attributes {
    const choices = [...this.choices].sort(([a], [b]) => a - b).map(([index, acc]) => ({
      index,
      finish_reason: acc.finish_reason,
      message: {
        ...acc.message,
        ...(acc.tools.size ? { tool_calls: [...acc.tools].sort(([a], [b]) => a - b).map(([, tool]) => tool) } : {}),
      },
    }));
    const attributes = completionAttributes({ id: this.id, model: this.model, choices, usage: this.usage });
    // Preserve the existing text output contract; structured output retains all choices.
    attributes[S.OUTPUT_VALUE] = choices[0]?.message.content ?? "";
    attributes[S.OUTPUT_MIME_TYPE] = MimeType.TEXT;
    return attributes;
  }
}

/** Observe only consumption requested by the application, keeping SDK helpers and brands. */
export function observeResponse<T>(
  promise: ResponsePromise<T>, span: Span, execContext: Context,
  attributes?: (result: T) => Attributes,
): ResponsePromise<T> {
  let ended = false;
  const end = (attributes: Attributes = {}, error?: unknown, cancelled = false) => {
    if (ended) return;
    ended = true;
    span.setAttributes(attributes);
    if (error !== undefined) {
      const exception = error instanceof Error ? error : new Error(String(error));
      span.recordException(exception);
      span.setStatus({ code: SpanStatusCode.ERROR, message: exception.message });
    } else if (!cancelled) {
      span.setStatus({ code: SpanStatusCode.OK });
    }
    span.end();
  };
  const fail = (error: unknown): never => { end({}, error); throw error; };
  const observed = new WeakMap<object, T>();
  const observe = (result: T): T => {
    if (attributes) {
      end(attributes(result));
      return result;
    }
    const completion = result as Result;
    if (observed.has(completion)) return observed.get(completion)!;
    if (Symbol.asyncIterator in completion) {
      const stream = observeStream(completion, execContext, end) as T;
      observed.set(completion, stream);
      return stream;
    }
    end(completionAttributes(completion));
    observed.set(completion, result);
    return result;
  };
  let parsed: Promise<T> | undefined;
  let parsingRequested = false;
  const parse = () => {
    parsingRequested = true;
    return parsed ??= promise.then(observe, fail);
  };
  return new Proxy(promise, {
    get(target, key) {
      // Calling then here (rather than when create returns) preserves the SDK's
      // lazy JSON parsing and prevents an unrequested background stream reader.
      if (key === "then") return (...args: Parameters<Promise<T>["then"]>) => parse().then(...args);
      if (key === "catch") return (...args: Parameters<Promise<T>["catch"]>) => parse().catch(...args);
      if (key === "finally") return (...args: Parameters<Promise<T>["finally"]>) => parse().finally(...args);
      if (key === "withResponse" && target.withResponse) {
        return () => {
          parsingRequested = true;
          return target.withResponse!().then(value => ({ ...value, data: observe(value.data) }), fail);
        };
      }
      if (key === "asResponse" && target.asResponse) {
        // Raw-only callers own the body. End a metadata-only span without
        // parsing, cloning, draining, or replacing their Response.
        return () => target.asResponse!().then(value => {
          if (!parsingRequested) end();
          return value;
        }, fail);
      }
      const value = Reflect.get(target, key, target);
      return typeof value === "function" && key !== "constructor" ? value.bind(target) : value;
    },
  });
}

function observeStream(
  stream: Stream,
  execContext: Context,
  end: (attributes?: Attributes, error?: unknown, cancelled?: boolean) => void,
): Stream {
  const acc = new Accumulator();
  let finished = false;
  let pending = 0;
  const finish = (error?: unknown, cancelled = false) => {
    if (finished) return;
    finished = true;
    stream.controller.signal.removeEventListener("abort", onAbort);
    end(acc.attributes(), error === undefined ? acc.error : error, cancelled);
  };
  const onAbort = () => {
    // SDK iterators abort in their error cleanup too. Let the pending next()
    // settle first so a real provider/parse error is not recorded as a cancel.
    if (!pending) finish(undefined, true);
  };
  stream.controller.signal.addEventListener("abort", onAbort, { once: true });
  if (stream.controller.signal.aborted) onAbort();

  // Stream's public constructor exists at the declared SDK floor and current
  // release. Reusing it keeps tee(), toReadableStream(), instanceof and the
  // exact controller, without depending on the SDK's private iterator field.
  const StreamClass = stream.constructor as new (iterator: () => AsyncIterator<Completion>, controller: AbortController) => Stream;
  return new StreamClass(() => {
    const iterator = stream[Symbol.asyncIterator]();
    const run = async (operation: () => Promise<IteratorResult<Completion>>, cancelled = false) => {
      pending++;
      try {
        const item = await context.with(execContext, operation);
        if (!finished && !item.done) acc.add(item.value);
        if (item.done || cancelled || stream.controller.signal.aborted) {
          finish(undefined, cancelled || stream.controller.signal.aborted);
        }
        return item;
      } catch (error) {
        finish(error);
        throw error;
      } finally {
        pending--;
      }
    };
    return {
      next: (...args) => run(() => iterator.next(...args)),
      return: value => run(() => iterator.return ? iterator.return(value) : Promise.resolve({ done: true, value }), true),
      throw: error => run(() => iterator.throw ? iterator.throw(error) : Promise.reject(error)),
      [Symbol.asyncIterator]() { return this; },
    };
  }, stream.controller);
}
