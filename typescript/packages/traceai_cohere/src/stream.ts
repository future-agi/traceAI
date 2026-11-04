import { context, Context } from "@opentelemetry/api";
import { performance } from "node:perf_hooks";
import { Data, object, list } from "./attributes";

type Finish = (response: Data, error?: unknown, cancelled?: boolean, timeToFirstTokenMs?: number) => void;

/** Ignore metadata/empty deltas. Inspect content even when capture is disabled,
 * without retaining it, so privacy does not change timing semantics.
 */
function hasGeneratedContent(value: unknown): boolean {
  const event = object(value);
  const type = event.type ?? event.eventType ?? event.event_type;
  const message = object(object(event.delta).message);
  const nonempty = (value: unknown) => typeof value === "string" && value.length > 0;
  const toolContent = (value: unknown) => {
    const tool = object(value);
    const fn = object(tool.function);
    return nonempty(tool.text) || nonempty(tool.name) || nonempty(tool.parameters) || nonempty(fn.name) || nonempty(fn.arguments);
  };
  switch (type) {
    case "text-generation": return nonempty(event.text);
    case "content-start":
    case "content-delta": {
      const content = object(message.content);
      return nonempty(content.text) || nonempty(content.thinking);
    }
    case "tool-plan-delta": return nonempty(message.toolPlan ?? message.tool_plan);
    case "tool-call-start":
    case "tool-call-delta": return toolContent(message.toolCalls ?? message.tool_calls);
    case "tool-calls-chunk": return toolContent(event.toolCallDelta ?? event.tool_call_delta);
    case "tool-calls-generation": return nonempty(event.text) || list(event.toolCalls ?? event.tool_calls).some(toolContent);
    default: return false;
  }
}

/** Aggregate content, not the event log. Consumers still receive original events. */
class ChatAccumulator {
  private text = "";
  private toolPlan = "";
  private content = new Map<number, Data>();
  private tools = new Map<number, Data>();
  private citations = new Map<number, Data>();
  private response: Data = {};
  constructor(private captureContent: boolean) {}

  add(value: unknown): boolean {
    const event = object(value);
    const type = event.type ?? event.eventType ?? event.event_type;
    const delta = object(event.delta);
    const message = object(delta.message);
    if (type === "message-start" || type === "stream-start") {
      const id = event.id ?? event.generationId ?? event.generation_id;
      if (typeof id === "string") this.response.id = id;
    }
    if (this.captureContent) {
      if ((type === "citation-start" || type === "citation-end") && message.citations) {
        const index = typeof event.index === "number" ? event.index : 0;
        this.citations.set(index, { ...this.citations.get(index), ...object(message.citations) });
      }
      if (type === "text-generation" && typeof event.text === "string") this.text += event.text;
      if (type === "content-start" || type === "content-delta") {
        const content = object(message.content);
        const index = typeof event.index === "number" ? event.index : 0;
        const previous = this.content.get(index) ?? {};
        const block: Data = { ...previous };
        // Keep reasoning separate from visible text, preserving block order and
        // partial content even when a stream ends before its terminal event.
        for (const key of ["text", "thinking"]) {
          if (typeof content[key] === "string") {
            block.type = content.type ?? block.type ?? key;
            block[key] = String(previous[key] ?? "") + content[key];
          }
        }
        if (block.type) this.content.set(index, block);
        if (typeof content.text === "string") this.text += content.text;
      }
      if (type === "tool-plan-delta") {
        const plan = message.toolPlan ?? message.tool_plan;
        if (typeof plan === "string") this.toolPlan += plan;
      }
      if (type === "tool-calls-chunk") {
        const call = object(event.toolCallDelta ?? event.tool_call_delta);
        if (typeof call.text === "string") this.toolPlan += call.text;
        const index = typeof call.index === "number" ? call.index : 0;
        const previous = this.tools.get(index) ?? {};
        this.tools.set(index, {
          ...previous,
          ...(typeof call.name === "string" ? { name: call.name } : {}),
          // These are JSON fragments, not objects: retain unfinished arguments.
          parameters: String(previous.parameters ?? "") + String(call.parameters ?? ""),
        });
      }
      if (type === "tool-calls-generation") {
        if (typeof event.text === "string") this.toolPlan = event.text;
        this.tools.clear();
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
    if (type === "stream-end") {
      const response = object(event.response);
      // V1's terminal text is authoritative after provisional tool chunks.
      if (this.toolPlan && typeof response.text === "string" && response.text) this.toolPlan = response.text;
      this.response = { ...this.response, ...response, finishReason: event.finishReason ?? event.finish_reason ?? response.finishReason ?? response.finish_reason };
    }
    if (type === "message-end") this.response = { ...this.response, usage: delta.usage, finishReason: delta.finishReason ?? delta.finish_reason };
    return type === "stream-end" || type === "message-end";
  }

  result(): Data {
    if (!this.captureContent) return {
      id: this.response.id, generationId: this.response.generationId ?? this.response.generation_id,
      usage: this.response.usage, meta: this.response.meta, finishReason: this.response.finishReason,
    };
    // V1's terminal response has authoritative text/tool calls; v2 only sends usage.
    return {
      text: this.text,
      toolCalls: [...this.tools].sort(([a], [b]) => a - b).map(([, call]) => call),
      ...(this.content.size || this.toolPlan || this.citations.size ? { message: {
        role: "assistant",
        ...(this.content.size ? { content: [...this.content].sort(([a], [b]) => a - b).map(([, block]) => block) } : {}),
        ...(this.toolPlan ? { toolPlan: this.toolPlan } : {}),
        ...(this.citations.size ? { citations: [...this.citations].sort(([a], [b]) => a - b).map(([, citation]) => citation) } : {}),
      } } : {}),
      ...this.response,
    };
  }
}

export function wrapStream<T extends object>(stream: T, execContext: Context, captureContent: boolean, finish: Finish, requestStart: number): T {
  const accumulator = new ChatAccumulator(captureContent);
  let timeToFirstTokenMs: number | undefined;
  const signal = (stream as { controller?: AbortController }).controller?.signal;
  let ended = false;
  const end = (error?: unknown, cancelled = false) => {
    if (ended) return;
    ended = true;
    signal?.removeEventListener("abort", onAbort);
    finish(accumulator.result(), error, cancelled, timeToFirstTokenMs);
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
              if (timeToFirstTokenMs === undefined && hasGeneratedContent(result.value)) {
                timeToFirstTokenMs = performance.now() - requestStart;
              }
              const terminal = accumulator.add(result.value);
              if (terminal) end();
            }
            if (!ended && (result.done || method === "return")) {
              // Terminal events finish the span above. A clean transport EOF
              // without one is still an incomplete generation, not success.
              end(method === "return" ? undefined : new Error("Cohere stream ended without a terminal event"), method === "return");
            }
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
