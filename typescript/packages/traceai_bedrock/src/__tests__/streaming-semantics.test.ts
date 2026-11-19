import { afterEach, describe, expect, it, jest } from "@jest/globals";
import { InvokeModelWithResponseStreamCommand } from "@aws-sdk/client-bedrock-runtime";
import { SpanStatusCode } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { SemanticConventions } from "@traceai/fi-semantic-conventions";
import { BedrockInstrumentation } from "../instrumentation";

const chunk = (data: unknown) => ({ chunk: { bytes: new TextEncoder().encode(JSON.stringify(data)) } });
const partial = chunk({ type: "content_block_delta", delta: { text: "partial" } });
const command = () => new InvokeModelWithResponseStreamCommand({
  modelId: "anthropic.claude-3-haiku-20240307-v1:0",
  body: JSON.stringify({ messages: [{ role: "user", content: "hello" }] }),
});

function harness(send: () => Promise<{ body: AsyncIterable<unknown> }>) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  class BedrockRuntimeClient { send(_command: unknown) { return send(); } }
  const instrumentation = new BedrockInstrumentation({
    instrumentationConfig: { enabled: false }, tracerProvider: provider,
  });
  instrumentation.manuallyInstrument({ BedrockRuntimeClient } as unknown as Parameters<BedrockInstrumentation["manuallyInstrument"]>[0]);
  return { exporter, client: new BedrockRuntimeClient() };
}

afterEach(() => { jest.useRealTimers(); });

describe("Bedrock demand-only span semantics", () => {
  it.each(["validation", "throttling", "auth"])("ends immediately when send rejects: %s", async (kind) => {
    const failure = new Error(`synthetic ${kind} failure`);
    const { client, exporter } = harness(async () => { throw failure; });
    await expect(client.send(command())).rejects.toBe(failure);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status).toEqual({ code: SpanStatusCode.ERROR, message: failure.message });
    expect(spans[0].events).toEqual(expect.arrayContaining([expect.objectContaining({ name: "exception" })]));
  });

  it("ends ERROR on the surfacing pull, retaining partial output without unhandled rejection", async () => {
    const failure = new Error("synthetic pull failure");
    const unhandled: unknown[] = [];
    const listener = (error: unknown) => { unhandled.push(error); };
    process.on("unhandledRejection", listener);
    let pulls = 0;
    async function* body() { pulls++; yield partial; pulls++; throw failure; }
    const { client, exporter } = harness(async () => ({ body: body() }));
    try {
      const response = await client.send(command());
      await new Promise<void>((resolve) => setImmediate(resolve));
      expect(pulls).toBe(0);
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      const iterator = response.body[Symbol.asyncIterator]();
      expect((await iterator.next()).value).toBe(partial);
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      await expect(iterator.next()).rejects.toBe(failure);
      await new Promise<void>((resolve) => setImmediate(resolve));
      expect(unhandled).toEqual([]);
      expect(pulls).toBe(2);
      const spans = exporter.getFinishedSpans();
      expect(spans).toHaveLength(1);
      expect(spans[0].status).toEqual({ code: SpanStatusCode.ERROR, message: failure.message });
      expect(spans[0].events).toEqual(expect.arrayContaining([expect.objectContaining({ name: "exception", attributes: expect.objectContaining({ "exception.message": failure.message }) })]));
      expect(JSON.parse(spans[0].attributes[SemanticConventions.OUTPUT_VALUE] as string).text).toBe("partial");
    } finally { process.removeListener("unhandledRejection", listener); }
  });

  it.each(["break", "return", "throw"])("ends UNSET on caller %s and does not pull the remainder", async (control) => {
    let pulls = 0;
    let closed = false;
    async function* body() {
      try { pulls++; yield partial; pulls++; yield partial; }
      finally { closed = true; }
    }
    const { client, exporter } = harness(async () => ({ body: body() }));
    const response = await client.send(command());
    if (control === "break") {
      for await (const event of response.body) { expect(event).toBe(partial); break; }
    } else {
      const iterator = response.body[Symbol.asyncIterator]();
      await iterator.next();
      if (control === "return") await iterator.return?.();
      else {
        const cancellation = new Error("caller cancelled");
        await expect(iterator.throw?.(cancellation)).rejects.toBe(cancellation);
      }
    }
    expect(closed).toBe(true);
    expect(pulls).toBe(1);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.UNSET);
    expect(spans[0].events.map(event => event.name)).toEqual(["stream.cancelled"]);
    expect(JSON.parse(spans[0].attributes[SemanticConventions.OUTPUT_VALUE] as string).text).toBe("partial");
  });

  it("measures TTFT at the first caller-pulled chunk and exports usage and finish reason on completion", async () => {
    jest.useFakeTimers({ doNotFake: ["nextTick", "setImmediate"] });
    const events = [
      chunk({ type: "message_start", message: { usage: { input_tokens: 3 } } }),
      partial,
      chunk({ type: "message_delta", delta: { stop_reason: "end_turn" }, usage: { output_tokens: 2 } }),
    ];
    let pulls = 0;
    async function* body() { for (const event of events) { pulls++; yield event; } }
    const { client, exporter } = harness(async () => ({ body: body() }));
    const response = await client.send(command());
    jest.advanceTimersByTime(2000);
    expect(pulls).toBe(0);
    const iterator = response.body[Symbol.asyncIterator]();
    expect((await iterator.next()).value).toBe(events[0]);
    jest.advanceTimersByTime(3000);
    expect((await iterator.next()).value).toBe(events[1]);
    expect((await iterator.next()).value).toBe(events[2]);
    expect((await iterator.next()).done).toBe(true);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.OK);
    expect(spans[0].attributes[SemanticConventions.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN]).toBe(2000);
    expect(spans[0].attributes[SemanticConventions.GEN_AI_RESPONSE_FINISH_REASONS]).toEqual(["end_turn"]);
    expect(spans[0].attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBe(3);
    expect(spans[0].attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]).toBe(2);
  });
});
