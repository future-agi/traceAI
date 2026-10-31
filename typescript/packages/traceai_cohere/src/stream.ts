import { context, Context } from "@opentelemetry/api";
import { Data, object, list } from "./attributes";

type Finish = (response: Data, error?: unknown, cancelled?: boolean) => void;

/** Aggregate content, not the event log. Consumers still receive original events. */
class ChatAccumulator {
  private text = "";
  private tools = new Map<number, Data>();
  private response: Data = {};
  constructor(private captureContent: boolean) {}

  add(value: unknown): boolean {
    const event = object(value);
    const type = event.type ?? event.eventType ?? event.event_type;
    const delta = object(event.delta);
    const message = object(delta.message);
    if (this.captureContent) {
      if (type === "text-generation" && typeof event.text === "string") this.text += event.text;
      if (type === "content-delta") {
        const text = object(message.content).text;
        if (typeof text === "string") this.text += text;
      }
      if (type === "tool-calls-generation") {
        list(event.toolCalls ?? event.tool_calls).forEach((call, index) => this.tools.set(index, object(call)));
      }
      if (type === "tool-call-start" || type === "tool-call-delta") {
        const call = object(message.toolCalls ?? message.tool_calls);
        const index = typeof event.index === "number" ? event.index : 0;
        const previous = this.tools.get(index) ?? {};
        const fn = object(call.function);
        const previousFn = object(previous.function);
        this.tools.set(index, {
          ...previous, ...call,
          function: { ...previousFn, ...fn, arguments: String(previousFn.arguments ?? "") + String(fn.arguments ?? "") },
        });
      }
    }
    if (type === "stream-end") this.response = object(event.response);
    if (type === "message-end") this.response = { usage: delta.usage, finishReason: delta.finishReason ?? delta.finish_reason };
    return type === "stream-end" || type === "message-end";
  }

  result(): Data {
    if (!this.captureContent) return { usage: this.response.usage, meta: this.response.meta };
    // V1's terminal response has authoritative text/tool calls; v2 only sends usage.
    return {
      text: this.text,
      toolCalls: [...this.tools.values()],
      ...this.response,
    };
  }
}

export function wrapStream<T extends object>(stream: T, execContext: Context, captureContent: boolean, finish: Finish): T {
  const accumulator = new ChatAccumulator(captureContent);
  const signal = (stream as { controller?: AbortController }).controller?.signal;
  let ended = false;
  const end = (error?: unknown, cancelled = false) => {
    if (ended) return;
    ended = true;
    signal?.removeEventListener("abort", onAbort);
    finish(accumulator.result(), error, cancelled);
  };
  const onAbort = () => end(undefined, true);
  signal?.addEventListener("abort", onAbort, { once: true });
  if (signal?.aborted) onAbort();

  return new Proxy(stream, {
    get(target, key) {
      if (key === Symbol.asyncIterator) return () => {
        let iterator: AsyncIterator<unknown>;
        try {
          iterator = context.with(execContext, () => (target as unknown as AsyncIterable<unknown>)[Symbol.asyncIterator]());
        } catch (error) {
          end(error ?? new Error("Cohere stream failed"));
          throw error;
        }
        const invoke = async (method: "next" | "return" | "throw", value?: unknown): Promise<IteratorResult<unknown>> => {
          try {
            const fn = iterator[method];
            if (!fn) {
              if (method === "throw") throw value;
              end(undefined, method === "return");
              return { done: true, value };
            }
            const result = await context.with(execContext, () => fn.call(iterator, value));
            if (!ended && !result.done) {
              const terminal = accumulator.add(result.value);
              if (terminal) {
                const event = object(result.value);
                const delta = object(event.delta);
                const reason = event.finishReason ?? event.finish_reason ?? delta.finishReason ?? delta.finish_reason;
                end(reason === "ERROR" || reason === "ERROR_TOXIC" ? new Error("Cohere stream failed") : undefined);
              }
            }
            if (result.done || method === "return") end(undefined, method === "return");
            return result;
          } catch (error) {
            end(error ?? new Error("Cohere stream failed"));
            throw error;
          }
        };
        return {
          next: (value?: unknown) => invoke("next", value),
          return: (value?: unknown) => invoke("return", value),
          throw: (error?: unknown) => invoke("throw", error),
          [Symbol.asyncIterator]() { return this; },
        };
      };
      const value = Reflect.get(target, key, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}
