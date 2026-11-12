import { beforeEach, describe, expect, it } from "@jest/globals";
import { InvokeModelWithResponseStreamCommand } from "@aws-sdk/client-bedrock-runtime";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { BedrockInstrumentation } from "../instrumentation";
import { safelySplitStream } from "../attributes/invoke-model-streaming-response-attributes";

function nextTurn(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}

async function collect(stream: AsyncIterable<unknown>): Promise<unknown[]> {
  const chunks: unknown[] = [];
  for await (const chunk of stream) {
    chunks.push(chunk);
  }
  return chunks;
}

function command(): InvokeModelWithResponseStreamCommand {
  return new InvokeModelWithResponseStreamCommand({
    modelId: "anthropic.claude-3-haiku-20240307-v1:0",
    contentType: "application/json",
    body: JSON.stringify({
      anthropic_version: "bedrock-2023-05-31",
      max_tokens: 8,
      messages: [{ role: "user", content: [{ type: "text", text: "hello" }] }],
    }),
  });
}

const chunk = { chunk: { bytes: new TextEncoder().encode("{}") } };

describe("T8-TS-CLOUD-002: Bedrock streaming errors", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });

  beforeEach(() => {
    exporter.reset();
  });

  function clientFor(body: AsyncIterable<unknown>) {
    // Fake only the SDK transport; exercise the real send instrumentation.
    class BedrockRuntimeClient {
      async send(_command: unknown) {
        return { body, contentType: "application/json" };
      }
    }
    const instrumentation = new BedrockInstrumentation({
      instrumentationConfig: { enabled: false },
      tracerProvider: provider,
    });
    instrumentation.manuallyInstrument({
      BedrockRuntimeClient,
    } as unknown as Parameters<BedrockInstrumentation["manuallyInstrument"]>[0]);
    return new BedrockRuntimeClient();
  }

  it.each([false, true])(
    "delayed iteration catches the original failure (yield chunk: %s)",
    async (yieldChunk) => {
      const failure = new Error("fake-stream-failure");
      let sourceFailed = false;
      const body = (async function* () {
        if (yieldChunk) {
          yield chunk;
        }
        await nextTurn();
        sourceFailed = true;
        throw failure;
      })();

      const response = await clientFor(body).send(command());
      // No iterator or error listener is attached to response.body during this
      // delay. Before the fix, destroy(failure) emits an unhandled error here.
      await nextTurn();
      await nextTurn();
      // Demand-only observation must not start the source during the delay.
      expect(sourceFailed).toBe(false);
      expect(exporter.getFinishedSpans()).toHaveLength(0);

      // No unhandled error/rejection may escape during the delay, and the
      // original failure must still surface on the caller's pull.
      await expect(collect(response.body)).rejects.toBe(failure);
      expect(sourceFailed).toBe(true);
      expect(exporter.getFinishedSpans()).toHaveLength(1);
      expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
      expect(exporter.getFinishedSpans()[0].status.message).toBe(failure.message);
      expect(exporter.getFinishedSpans()[0].events).toEqual(expect.arrayContaining([
        expect.objectContaining({ name: "exception" }),
      ]));
    },
  );

  it("immediate iteration preserves the chunk and original failure", async () => {
    const failure = new Error("fake-stream-failure");
    const body = (async function* () {
      yield chunk;
      await nextTurn();
      throw failure;
    })();
    const response = await clientFor(body).send(command());
    const received: unknown[] = [];

    await expect(
      (async () => {
        for await (const event of response.body) {
          received.push(event);
        }
      })(),
    ).rejects.toBe(failure);
    expect(received).toHaveLength(1);
    expect(received[0]).toBe(chunk);
  });

  it("delayed successful iteration preserves chunks and response fields", async () => {
    const secondChunk = { chunk: { bytes: new TextEncoder().encode("{}") } };
    const body = (async function* () {
      yield chunk;
      yield secondChunk;
    })();
    const response = await clientFor(body).send(command());
    await nextTurn();

    const received = await collect(response.body);
    expect(received).toHaveLength(2);
    expect(received[0]).toBe(chunk);
    expect(received[1]).toBe(secondChunk);
    expect(response.contentType).toBe("application/json");
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.OK);
  });

  it("retains the failure when both tee consumers start late", async () => {
    const failure = new Error("fake-stream-failure");
    const body = (async function* () {
      yield chunk;
      await nextTurn();
      throw failure;
    })();
    const { instrumentationStream, userStream } = safelySplitStream({
      originalStream: body,
    });
    await nextTurn();
    await nextTurn();

    expect(instrumentationStream).toBeDefined();
    if (!instrumentationStream) {
      throw new Error("Expected an instrumentation stream");
    }
    await expect(collect(instrumentationStream)).rejects.toBe(failure);
    await expect(collect(userStream)).rejects.toBe(failure);
  });
});
