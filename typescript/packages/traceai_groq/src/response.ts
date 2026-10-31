import { Attributes, Context, Span, SpanStatusCode, context } from "@opentelemetry/api";
import { SemanticConventions as S, MimeType } from "@traceai/fi-semantic-conventions";
import { safelyJSONStringify } from "@traceai/fi-core";

interface Usage { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number }
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
  x_groq?: { usage?: Usage | null };
}
interface Stream extends AsyncIterable<Completion> {
  controller: AbortController;
}
type Result = Completion | Stream;
interface ResponsePromise extends Promise<Result> {
  asResponse?(): Promise<unknown>;
  withResponse?(): Promise<{ data: Result; [key: string]: unknown }>;
}

function usageAttributes(usage?: Usage | null): Attributes {
  return {
    [S.LLM_TOKEN_COUNT_PROMPT]: usage?.prompt_tokens,
    [S.LLM_TOKEN_COUNT_COMPLETION]: usage?.completion_tokens,
    [S.LLM_TOKEN_COUNT_TOTAL]: usage?.total_tokens,
  };
}

function completionAttributes(result: Completion): Attributes {
  const choices = [...result.choices].sort((a, b) => a.index - b.index);
  return {
    [S.OUTPUT_VALUE]: safelyJSONStringify(result) ?? undefined,
    [S.OUTPUT_MIME_TYPE]: MimeType.JSON,
    [S.RAW_OUTPUT]: safelyJSONStringify(result) ?? undefined,
    [S.LLM_OUTPUT_MESSAGES]: safelyJSONStringify(choices.map(c => c.message)) ?? undefined,
    [S.LLM_MODEL_NAME]: result.model,
    [S.GEN_AI_RESPONSE_MODEL]: result.model,
    [S.GEN_AI_RESPONSE_ID]: result.id,
    [S.GEN_AI_RESPONSE_FINISH_REASONS]: safelyJSONStringify(choices.map(c => c.finish_reason).filter(Boolean)) ?? undefined,
    ...usageAttributes(result.usage ?? result.x_groq?.usage),
  };
}

/** Aggregate by SDK indexes, not by position within a delta. No chunk buffer or second reader. */
class Accumulator {
  private choices = new Map<number, { message: Message; tools: Map<number, ToolCall>; finish_reason?: string | null }>();
  private id?: string;
  private model?: string;
  private usage?: Usage;

  add(chunk: Completion) {
    this.id = chunk.id ?? this.id;
    this.model = chunk.model ?? this.model;
    const usage = chunk.usage ?? chunk.x_groq?.usage;
    if (usage) this.usage = { ...this.usage, ...usage };
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
export function observeResponse(promise: ResponsePromise, span: Span, execContext: Context): ResponsePromise {
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
  const observed = new WeakMap<object, Result>();
  const observe = (result: Result): Result => {
    if (observed.has(result)) return observed.get(result)!;
    if (Symbol.asyncIterator in result) {
      const stream = observeStream(result, execContext, end);
      observed.set(result, stream);
      return stream;
    }
    end(completionAttributes(result));
    observed.set(result, result);
    return result;
  };
  let parsed: Promise<Result> | undefined;
  let parsingRequested = false;
  const parse = () => {
    parsingRequested = true;
    return parsed ??= promise.then(observe, fail);
  };
  return new Proxy(promise, {
    get(target, key) {
      // Calling then here (rather than when create returns) preserves the SDK's
      // lazy JSON parsing and prevents an unrequested background stream reader.
      if (key === "then") return (...args: Parameters<Promise<Result>["then"]>) => parse().then(...args);
      if (key === "catch") return (...args: Parameters<Promise<Result>["catch"]>) => parse().catch(...args);
      if (key === "finally") return (...args: Parameters<Promise<Result>["finally"]>) => parse().finally(...args);
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
    end(acc.attributes(), error, cancelled);
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
    };
  }, stream.controller);
}
