import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { InvokeModelWithResponseStreamCommand } from "@aws-sdk/client-bedrock-runtime";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { BedrockInstrumentation } from "../instrumentation";

function chunk(data: Record<string, unknown>) {
  return { chunk: { bytes: new TextEncoder().encode(JSON.stringify(data)) } };
}

const nextTurn = () => new Promise<void>((resolve) => setImmediate(resolve));

describe("T8-TS-CLOUD-003: Bedrock streaming lifecycle", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });

  beforeEach(() => exporter.reset());

  async function responseFor(body: AsyncIterable<unknown>) {
    const response = { body, requestId: "offline-response" };
    class BedrockRuntimeClient {
      send(_command: unknown) {
        return Promise.resolve(response);
      }
    }
    const instrumentation = new BedrockInstrumentation({
      instrumentationConfig: { enabled: false },
      tracerProvider: provider,
    });
    instrumentation.manuallyInstrument({
      BedrockRuntimeClient,
    } as unknown as Parameters<BedrockInstrumentation["manuallyInstrument"]>[0]);
    return new BedrockRuntimeClient().send(
      new InvokeModelWithResponseStreamCommand({
        modelId: "amazon.titan-text-express-v1",
        body: JSON.stringify({ inputText: "offline prompt" }),
      }),
    );
  }

  it("does not pull before consumption or ahead of a slow consumer", async () => {
    const event = chunk({ outputText: "part", tokenCount: 1 });
    let pulled = 0;
    async function* body() {
      for (let i = 0; i < 100; i++) {
        pulled++;
        yield event;
      }
    }
    const response = await responseFor(body());
    const iterator = response.body[Symbol.asyncIterator]();
    try {
      await nextTurn();
      expect(pulled).toBe(0);
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      expect((await iterator.next()).value).toBe(event);
      await nextTurn();
      expect(pulled).toBe(1);
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      expect((await iterator.next()).value).toBe(event);
      await nextTurn();
      expect(pulled).toBe(2);
    } finally {
      await iterator.return?.();
    }
  });

  it("closes upstream on break and never drains the gated remainder", async () => {
    const event = chunk({ outputText: "first", tokenCount: 1 });
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let pulled = 0;
    let closed = false;
    async function* body() {
      try {
        pulled++;
        yield event;
        await gate;
        for (let i = 1; i < 64; i++) {
          pulled++;
          yield event;
        }
      } finally {
        closed = true;
      }
    }
    const response = await responseFor(body());
    let closedOnBreak = false;
    try {
      for await (const received of response.body) {
        expect(received).toBe(event);
        break;
      }
      closedOnBreak = closed;
    } finally {
      release();
      await nextTurn();
    }
    expect(closedOnBreak).toBe(true);
    expect(pulled).toBe(1);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    // Cancellation must end the span without claiming a complete generation.
    expect(spans[0].status.code).toBe(SpanStatusCode.UNSET);
    expect(JSON.parse(spans[0].attributes[SemanticConventions.OUTPUT_VALUE] as string).text)
      .toBe("first");
  });

  it("forwards next, handled throw, and return with their values and receiver", async () => {
    const first = { done: false as const, value: chunk({ outputText: "first" }) };
    const recovered = { done: false as const, value: chunk({ outputText: "recovered" }) };
    const returned = { done: true as const, value: "upstream-return" };
    let calls = 0;
    const next = jest.fn(async (_value?: unknown) => ++calls === 1 ? first : returned);
    const thrown = jest.fn(async (_error?: unknown) => recovered);
    const finish = jest.fn(async (_value?: unknown) => returned);
    const upstream: AsyncIterableIterator<unknown> = {
      next,
      throw: thrown,
      return: finish,
      [Symbol.asyncIterator]() { return this; },
    };
    const response = await responseFor(upstream);
    const iterator = response.body[Symbol.asyncIterator]();
    const error = new Error("consumer control");
    expect(await iterator.next("resume")).toBe(first);
    expect(await iterator.throw?.(error)).toBe(recovered);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    expect(await iterator.return?.("stop")).toBe(returned);
    expect(next).toHaveBeenCalledTimes(1);
    expect(next).toHaveBeenCalledWith("resume");
    expect(thrown).toHaveBeenCalledWith(error);
    expect(finish).toHaveBeenCalledWith("stop");
    expect(next.mock.contexts[0]).toBe(upstream);
    expect(thrown.mock.contexts[0]).toBe(upstream);
    expect(finish.mock.contexts[0]).toBe(upstream);
    // Repeated terminal operations must not export another span.
    await iterator.return?.("again");
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("forwards return before the first next and ends the cancelled span", async () => {
    const returned = { done: true as const, value: "cancelled" };
    const next = jest.fn(async () => returned);
    const finish = jest.fn(async (_value?: unknown) => returned);
    const upstream: AsyncIterableIterator<unknown> = {
      next,
      return: finish,
      [Symbol.asyncIterator]() { return this; },
    };
    const response = await responseFor(upstream);
    const iterator = response.body[Symbol.asyncIterator]();
    expect(await iterator.return?.("cancelled")).toBe(returned);
    expect(next).not.toHaveBeenCalled();
    expect(finish).toHaveBeenCalledWith("cancelled");
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.UNSET);
  });

  it("preserves events and completed output attributes without a second consumer", async () => {
    const events = [
      chunk({ outputText: "hello ", tokenCount: 1 }),
      { chunk: { bytes: new TextEncoder().encode("not json") } },
      chunk({
        outputText: "world",
        tokenCount: 1,
        "amazon-bedrock-invocationMetrics": { inputTokenCount: 3, outputTokenCount: 2 },
      }),
    ];
    async function* body() { yield* events; }
    const response = await responseFor(body());
    expect(response.requestId).toBe("offline-response");
    const received = [];
    for await (const event of response.body) received.push(event);
    expect(received).toHaveLength(events.length);
    received.forEach((event, index) => expect(event).toBe(events[index]));
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.OK);
    expect(JSON.parse(spans[0].attributes[SemanticConventions.OUTPUT_VALUE] as string).text)
      .toBe("hello world");
    expect(spans[0].attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBe(3);
    expect(spans[0].attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]).toBe(2);
  });

  it("propagates the original upstream error and ends the span once", async () => {
    const error = new Error("offline upstream failure");
    async function* body() {
      yield chunk({ outputText: "partial" });
      throw error;
    }
    const response = await responseFor(body());
    const iterator = response.body[Symbol.asyncIterator]();
    await iterator.next();
    await expect(iterator.next()).rejects.toBe(error);
    await iterator.return?.();
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status).toEqual({
      code: SpanStatusCode.ERROR,
      message: "offline upstream failure",
    });
  });

  it("preserves a return failure rather than replacing or swallowing it", async () => {
    const error = new Error("offline close failure");
    let calls = 0;
    const finish = jest.fn(async () => { throw error; });
    const upstream: AsyncIterableIterator<unknown> = {
      async next() {
        return ++calls === 1
          ? { done: false, value: chunk({ outputText: "partial" }) }
          : { done: true, value: undefined };
      },
      return: finish,
      [Symbol.asyncIterator]() { return this; },
    };
    const response = await responseFor(upstream);
    const iterator = response.body[Symbol.asyncIterator]();
    await iterator.next();
    await expect(iterator.return?.()).rejects.toBe(error);
    expect(finish).toHaveBeenCalledTimes(1);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status).toEqual({
      code: SpanStatusCode.ERROR,
      message: "offline close failure",
    });
  });
});
