import { Attributes, Span, SpanStatusCode } from "@opentelemetry/api";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { safelyJSONStringify } from "@traceai/fi-core";

// Structural interfaces deliberately avoid depending on one OpenAI major's internals.
interface APIPromise<T> extends Promise<T> {
  _thenUnwrap?: (transform: (value: T) => T) => APIPromise<T>;
  parse: () => Promise<T>;
  asResponse: () => Promise<{ status: number }>;
  responsePromise: Promise<unknown>;
}

export function observeResponse<T>(
  promise: Promise<T>,
  onResult: (result: T) => T,
  onError: (error: unknown) => void,
  onRaw: (status: number) => void,
): Promise<T> {
  const api = promise as APIPromise<T>;
  if (typeof api._thenUnwrap !== "function") {
    return promise.then(onResult).catch((error: unknown) => {
      onError(error);
      throw error;
    });
  }
  // The SDK's transformation keeps lazy parsing, response helpers and request metadata.
  const wrapped = api._thenUnwrap(onResult);
  let parsing = false;
  const parse = wrapped.parse;
  wrapped.parse = function () {
    parsing = true;
    return parse.call(this).catch((error: unknown) => {
      onError(error);
      throw error;
    });
  };
  const asResponse = wrapped.asResponse;
  wrapped.asResponse = function () {
    return asResponse.call(this).then((response) => {
      // withResponse() requests parsing first; only raw-only access ends at headers.
      if (!parsing) onRaw(response.status);
      return response;
    });
  };
  // Observe rejection without consuming a successful body or starting a stream reader.
  void api.responsePromise.then(undefined, onError);
  return wrapped;
}

export function spanEnder(span: Span) {
  let ended = false;
  return (
    error?: unknown,
    attributes?: Attributes,
    status = SpanStatusCode.OK,
  ) => {
    if (ended) return;
    ended = true;
    if (attributes) span.setAttributes(attributes);
    if (error !== undefined) {
      const exception =
        error instanceof Error ? error : new Error(String(error));
      span.recordException(exception);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: exception.message,
      });
    } else {
      span.setStatus({ code: status });
    }
    span.end();
  };
}

export interface StreamChunk {
  id?: string;
  model?: string;
  choices?: Array<{
    index: number;
    text?: string;
    finish_reason?: string | null;
    delta?: {
      role?: string;
      content?: string | null;
      tool_calls?: Array<{
        index: number;
        id?: string;
        type?: string;
        function?: { name?: string; arguments?: string };
      }>;
    };
  }>;
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  } | null;
}
interface Stream<T> extends AsyncIterable<T> {
  iterator?: () => AsyncIterator<T>;
  controller?: AbortController;
}

/** Observe the native iterator, not a tee or a replacement Stream object. */
export function observeStream<T extends StreamChunk>(
  stream: Stream<T>,
  finish: ReturnType<typeof spanEnder>,
  chat: boolean,
  hideOutputPayload = false,
): Stream<T> {
  const messages = new Map<
    number,
    {
      role: string;
      content: string;
      tool_calls: Array<{
        id: string;
        type: string;
        function: { name: string; arguments: string };
      }>;
    }
  >();
  const reasons = new Map<number, string>();
  const metadata: Attributes = {};
  // Bound telemetry storage independently of the consumer's stream size.
  const limit = 64 * 1024;
  let captured = 0;
  const append = (value: string | undefined | null) => {
    const part = (value ?? "").slice(0, Math.max(0, limit - captured));
    captured += part.length;
    return part;
  };
  const capture = (chunk: T) => {
    if (chunk.id) metadata[S.GEN_AI_RESPONSE_ID] = chunk.id;
    if (chunk.model) {
      metadata[S.LLM_MODEL_NAME] = chunk.model;
      metadata[S.GEN_AI_RESPONSE_MODEL] = chunk.model;
    }
    if (chunk.usage) {
      metadata[S.LLM_TOKEN_COUNT_PROMPT] = chunk.usage.prompt_tokens;
      metadata[S.LLM_TOKEN_COUNT_COMPLETION] = chunk.usage.completion_tokens;
      metadata[S.LLM_TOKEN_COUNT_TOTAL] = chunk.usage.total_tokens;
    }
    for (const choice of chunk.choices ?? []) {
      if (
        !Number.isInteger(choice.index) ||
        choice.index < 0 ||
        choice.index >= 128
      )
        continue;
      if (choice.finish_reason) reasons.set(choice.index, choice.finish_reason);
      if (hideOutputPayload) continue;
      let message = messages.get(choice.index);
      if (!message) {
        message = { role: "assistant", content: "", tool_calls: [] };
        messages.set(choice.index, message);
      }
      message.content += append(chat ? choice.delta?.content : choice.text);
      for (const call of choice.delta?.tool_calls ?? []) {
        if (
          !Number.isInteger(call.index) ||
          call.index < 0 ||
          call.index >= 128
        )
          continue;
        const tool = (message.tool_calls[call.index] ??= {
          id: "",
          type: "function",
          function: { name: "", arguments: "" },
        });
        if (call.id) tool.id = append(call.id);
        tool.function.name += append(call.function?.name);
        tool.function.arguments += append(call.function?.arguments);
      }
    }
  };
  let pending = 0;
  const end = (error?: unknown, completed = false) => {
    // A normalizer's throw() first closes its source; wait for the outer error.
    if (pending > 0 && error === undefined && !completed) return;
    stream.controller?.signal.removeEventListener("abort", aborted);
    const output = [...messages.entries()]
      .sort(([a], [b]) => a - b)
      .map(([, message]) => ({
        ...message,
        tool_calls: message.tool_calls.filter(Boolean),
      }));
    finish(
      error,
      {
        ...metadata,
        ...(!hideOutputPayload
          ? {
              [S.OUTPUT_VALUE]: output[0]?.content ?? "",
              [S.OUTPUT_MIME_TYPE]: "text/plain",
              ...(chat
                ? { [S.LLM_OUTPUT_MESSAGES]: safelyJSONStringify(output) ?? "[]" }
                : {}),
            }
          : {}),
        [S.GEN_AI_RESPONSE_FINISH_REASONS]:
          safelyJSONStringify([...reasons.values()]) ?? "[]",
      },
      completed ? SpanStatusCode.OK : SpanStatusCode.UNSET,
    );
  };
  // SDK iterator cleanup can abort before rejecting next(); let that rejection win.
  const aborted = () => {
    if (pending === 0) end();
  };
  stream.controller?.signal.addEventListener("abort", aborted, { once: true });
  if (stream.controller?.signal.aborted) end();

  const observeIterator = (
    factory: () => AsyncIterator<T>,
    captureValues = true,
  ) =>
    function (this: Stream<T>): AsyncIterableIterator<T> {
      const iterator = factory.call(this);
      return {
        [Symbol.asyncIterator]() {
          return this;
        },
        async next(...args: [] | [unknown]) {
          pending++;
          try {
            const result = await iterator.next(...args);
            if (result.done) end(undefined, !stream.controller?.signal.aborted);
            else if (captureValues) capture(result.value);
            return result;
          } catch (error) {
            end(error);
            throw error;
          } finally {
            pending--;
            if (stream.controller?.signal.aborted) end();
          }
        },
        async return(value?: unknown) {
          pending++;
          try {
            return iterator.return
              ? await iterator.return(value)
              : { done: true as const, value };
          } catch (error) {
            end(error);
            throw error;
          } finally {
            pending--;
            end();
          }
        },
        async throw(error?: unknown) {
          pending++;
          try {
            if (iterator.throw) return await iterator.throw(error);
            throw error;
          } finally {
            pending--;
            end(error);
          }
        },
      };
    };
  // Modern tee()/toReadableStream() use iterator; the 4.0 floor only exposes Symbol.asyncIterator.
  if (stream.iterator) {
    stream.iterator = observeIterator(stream.iterator);
    // Newer Chat resources add a normalization generator over the native iterator.
    // Observe its early return too, without capturing its chunks a second time.
    if (Object.prototype.hasOwnProperty.call(stream, Symbol.asyncIterator)) {
      stream[Symbol.asyncIterator] = observeIterator(
        stream[Symbol.asyncIterator],
        false,
      );
    }
  } else
    stream[Symbol.asyncIterator] = observeIterator(
      stream[Symbol.asyncIterator],
    );
  return stream;
}
