/**
 * The legacy instrumentor against the real peer SDK (@anthropic-ai/sdk 0.27.3), with a
 * synthetic fetch. No network, no credentials.
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach } from "@jest/globals";
import Anthropic from "@anthropic-ai/sdk";
import { Stream } from "@anthropic-ai/sdk/streaming";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { AnthropicInstrumentation, isPatched } from "../instrumentation";

const params = {
  model: "synthetic-model",
  max_tokens: 32,
  messages: [{ role: "user" as const, content: "PRIVATE_INPUT_CANARY" }],
};
const message = {
  id: "msg_synthetic",
  type: "message",
  role: "assistant",
  model: params.model,
  content: [{ type: "text", text: "PRIVATE_OUTPUT_CANARY" }],
  stop_reason: "end_turn",
  stop_sequence: null,
  usage: { input_tokens: 7, output_tokens: 3, cache_creation_input_tokens: 4, cache_read_input_tokens: 5 },
};
const events = [
  { type: "message_start", message: { ...message, content: [], stop_reason: null, usage: { ...message.usage, output_tokens: 0 } } },
  { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } },
  { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "PART1_" } },
  { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "PART2" } },
  { type: "content_block_stop", index: 0 },
  { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: 3 } },
  { type: "message_stop" },
];

// "stall": send the first two events, then hold the body open until the request is aborted.
let mode: "full" | "stall" = "full";
const syntheticFetch = async (_url: unknown, init: any) => {
  const body = JSON.parse(init.body);
  const headers = { "request-id": "req_synthetic" };
  if (body.model === "synthetic-error") {
    return new Response(
      JSON.stringify({ type: "error", error: { type: "invalid_request_error", message: "bad" } }),
      { status: 400, headers: { ...headers, "content-type": "application/json" } },
    );
  }
  if (body.model === "synthetic-empty") {
    // A valid HTTP response with no body: the SDK resolves the call with null.
    return new Response(null, { status: 204, headers });
  }
  if (!body.stream) {
    return new Response(JSON.stringify(message), { status: 200, headers: { ...headers, "content-type": "application/json" } });
  }
  const signal: AbortSignal | undefined = init.signal;
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    async start(controller) {
      for (let i = 0; i < events.length; i++) {
        if (body.model === "synthetic-stream-error" && i === 1) {
          controller.enqueue(encoder.encode('event: error\ndata: {"type":"error","error":{"type":"api_error","message":"synthetic SSE failure"}}\n\n'));
          controller.close();
          return;
        }
        // "synthetic-truncated": the server closes the body cleanly before message_stop.
        if (body.model === "synthetic-truncated" && i === 3) break;
        if (mode === "stall" && i === 2) {
          await new Promise<void>((resolve) => {
            if (signal?.aborted) resolve();
            signal?.addEventListener("abort", () => resolve());
          });
          controller.error(Object.assign(new Error("aborted"), { name: "AbortError" }));
          return;
        }
        controller.enqueue(encoder.encode(`event: ${events[i].type}\ndata: ${JSON.stringify(events[i])}\n\n`));
        await new Promise((resolve) => setTimeout(resolve, 2));
      }
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { ...headers, "content-type": "text/event-stream" } });
};

const memory = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
const instrumentation = new AnthropicInstrumentation({ instrumentationConfig: { enabled: false } });
const client = new Anthropic({ apiKey: "synthetic", baseURL: "https://offline.invalid", maxRetries: 0, fetch: syntheticFetch as any });

async function finishedSpans(count = 1) {
  for (let i = 0; i < 100 && memory.getFinishedSpans().length < count; i++) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  return memory.getFinishedSpans();
}

describe("legacy SDK 0.27.3 (declared peer)", () => {
  beforeAll(() => {
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
    instrumentation.manuallyInstrument(Anthropic);
    expect(isPatched()).toBe(true);
  });
  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });
  beforeEach(() => {
    mode = "full";
    memory.reset();
  });

  it("keeps the SDK APIPromise: withResponse() returns data and the raw response", async () => {
    const promise = client.messages.create(params);
    expect(typeof promise.withResponse).toBe("function");
    const { data, response } = await promise.withResponse();
    expect(data.id).toBe(message.id);
    expect(response.status).toBe(200);
    const [span] = await finishedSpans();
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(span.attributes["gen_ai.usage.output_tokens"]).toBe(3);
  });

  it("keeps the SDK APIPromise: asResponse() leaves the body unread and ends the span", async () => {
    const raw = await client.messages.create(params).asResponse();
    expect(raw.bodyUsed).toBe(false);
    expect(((await raw.json()) as { id: string }).id).toBe(message.id);
    const spans = await finishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.OK);
  });

  it("still records an awaited call once", async () => {
    expect((await client.messages.create(params)).id).toBe(message.id);
    const spans = await finishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes["fi.span.kind"]).toBe("LLM");
  });

  it("keeps withResponse() on a streamed call and records the full stream", async () => {
    const { data: stream } = await client.messages.create({ ...params, stream: true }).withResponse();
    const received = [];
    for await (const event of stream) received.push(event);
    expect(received).toHaveLength(events.length);
    const [span] = await finishedSpans();
    expect(span.status.code).toBe(SpanStatusCode.OK);
    expect(span.attributes["output.value"]).toBe("PART1_PART2");
  });

  it("keeps the messages.stream() helper working", async () => {
    const final = await client.messages.stream(params).finalMessage();
    expect(final.content[0]).toMatchObject({ text: "PART1_PART2" });
    const [span] = await finishedSpans();
    expect(span.status.code).toBe(SpanStatusCode.OK);
  });

  it("does not acquire an iterator or consume events until the application reads", async () => {
    const spy = jest.spyOn(Stream.prototype, Symbol.asyncIterator);
    try {
      const stream = await client.messages.create({ ...params, stream: true });
      await new Promise(resolve => setTimeout(resolve, 30));
      expect(spy).not.toHaveBeenCalled();
      expect(memory.getFinishedSpans()).toHaveLength(0);
      stream.controller.abort();
      expect((await finishedSpans())[0].status.code).toBe(SpanStatusCode.UNSET);
    } finally {
      spy.mockRestore();
    }
  });

  it("settles an abort that fires while a next result is being delivered", async () => {
    const original = Stream.prototype[Symbol.asyncIterator];
    const spy = jest.spyOn(Stream.prototype, Symbol.asyncIterator).mockImplementation(function (this: Stream<unknown>) {
      const iterator = original.call(this);
      const next = iterator.next.bind(iterator);
      iterator.next = async (...args) => {
        const result = await next(...args);
        this.controller.abort();
        return result;
      };
      return iterator;
    });
    try {
      const stream = await client.messages.create({ ...params, stream: true });
      const first = await stream[Symbol.asyncIterator]().next();
      expect(first.value.type).toBe("message_start");
      const spans = await finishedSpans();
      expect(spans).toHaveLength(1);
      expect(spans[0].status.code).toBe(SpanStatusCode.UNSET);
    } finally {
      spy.mockRestore();
    }
  });

  it("forwards early-break cancellation instead of continuing a background tee", async () => {
    mode = "stall";
    const stream = await client.messages.create({ ...params, stream: true });
    for await (const event of stream) {
      expect(event.type).toBe("message_start");
      break;
    }
    expect(stream.controller.signal.aborted).toBe(true);
    const spans = await finishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.UNSET);
  });

  it("forwards throw to the SDK iterator and ends cancellation once", async () => {
    mode = "stall";
    const stream = await client.messages.create({ ...params, stream: true });
    const iterator = stream[Symbol.asyncIterator]();
    await iterator.next();
    const error = new Error("caller stopped");
    await expect(iterator.throw!(error)).rejects.toBe(error);
    expect(stream.controller.signal.aborted).toBe(true);
    const spans = await finishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.UNSET);
  });

  it.each([false, true])("includes cache usage independently of raw output (stream=%s)", async streaming => {
    const result = await client.messages.create({ ...params, stream: streaming });
    if (streaming) for await (const _event of result as Stream<Anthropic.MessageStreamEvent>) { /* drain */ }
    const [span] = await finishedSpans();
    expect(span.attributes).toMatchObject({
      "gen_ai.usage.input_tokens": 16,
      "gen_ai.usage.output_tokens": 3,
      "gen_ai.usage.total_tokens": 19,
      "gen_ai.usage.cache_write_tokens": 4,
      "gen_ai.usage.cache_read_tokens": 5,
    });
  });

  it("does not mark a stream aborted through its controller as OK", async () => {
    mode = "stall";
    const stream = await client.messages.create({ ...params, stream: true });
    const iterator = stream[Symbol.asyncIterator]();
    await iterator.next();
    stream.controller.abort();
    while (!(await iterator.next()).done);
    const [span] = await finishedSpans();
    expect(span.status.code).toBe(SpanStatusCode.UNSET);
    expect(span.attributes["gen_ai.response.finish_reasons"]).toBeUndefined();
  });

  it("does not mark a stream aborted through the request signal as OK", async () => {
    mode = "stall";
    const abort = new AbortController();
    const stream = await client.messages.create({ ...params, stream: true }, { signal: abort.signal });
    const iterator = stream[Symbol.asyncIterator]();
    await iterator.next();
    abort.abort();
    while (!(await iterator.next()).done);
    const [span] = await finishedSpans();
    expect(span.status.code).toBe(SpanStatusCode.UNSET);
  });

  it("keeps OK when the application aborts after the stream completed", async () => {
    const stream = await client.messages.create({ ...params, stream: true });
    for await (const _event of stream) { /* drain */ }
    stream.controller.abort();
    const [span] = await finishedSpans();
    expect(span.status.code).toBe(SpanStatusCode.OK);
  });

  it("marks a stream that ends before message_stop without an abort as ERROR", async () => {
    const stream = await client.messages.create({ ...params, model: "synthetic-truncated", stream: true });
    const received = [];
    for await (const event of stream) received.push(event);
    expect(received).toHaveLength(3);
    const spans = await finishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status).toEqual({ code: SpanStatusCode.ERROR, message: "The stream ended before message_stop." });
    expect(spans[0].attributes["gen_ai.response.finish_reasons"]).toBeUndefined();
    expect(spans[0].attributes["output.value"]).toBe("PART1_");
  });

  it("returns the SDK's null result for an empty response and still ends the span", async () => {
    await expect(client.messages.create({ ...params, model: "synthetic-empty" })).resolves.toBeNull();
    const spans = await finishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.OK);
    expect(spans[0].attributes["output.value"]).toBeUndefined();
  });

  it("returns the SDK's null result through withResponse() and still ends the span", async () => {
    const { data, response } = await client.messages.create({ ...params, model: "synthetic-empty" }).withResponse();
    expect(data).toBeNull();
    expect(response.status).toBe(204);
    const spans = await finishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.OK);
  });

  it("records SSE errors as errors even when the SDK aborts during cleanup", async () => {
    const stream = await client.messages.create({ ...params, model: "synthetic-stream-error", stream: true });
    await expect((async () => {
      for await (const _event of stream) { /* consume */ }
    })()).rejects.toThrow("synthetic SSE failure");
    expect(stream.controller.signal.aborted).toBe(true);
    const spans = await finishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
  });

  it("records API errors and keeps the SDK error type", async () => {
    await expect(client.messages.create({ ...params, model: "synthetic-error" })).rejects.toBeInstanceOf(
      Anthropic.BadRequestError,
    );
    const spans = await finishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
  });

  // Jest turns an unhandled rejection into a test failure, so it cannot host the dropped-call
  // cases: contract/native-boundary.cjs checks those against the real floor and latest SDKs.
  it("records an awaited asResponse() failure once and keeps the SDK error type", async () => {
    await expect(client.messages.create({ ...params, model: "synthetic-error" }).asResponse()).rejects.toBeInstanceOf(
      Anthropic.BadRequestError,
    );
    const spans = await finishedSpans();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(memory.getFinishedSpans()).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
  });

  it("records API errors once when the application uses withResponse()", async () => {
    const promise = client.messages.create({ ...params, model: "synthetic-error" });
    promise.catch(() => {});
    expect(typeof promise.withResponse).toBe("function");
    await expect(promise.withResponse()).rejects.toBeInstanceOf(Anthropic.BadRequestError);
    const spans = await finishedSpans();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(memory.getFinishedSpans()).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
  });
});
