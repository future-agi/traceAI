import { describe, it, expect, beforeEach } from "@jest/globals";
import { runInNewContext } from "vm";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  ReadableSpan,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { ConverseCommand, InvokeModelCommand } from "@aws-sdk/client-bedrock-runtime";
import { TraceConfigOptions } from "@traceai/fi-core";
import { BedrockInstrumentation } from "../instrumentation";

const PROMPT = "my secret prompt: the launch code is 0000";

/** An error from another realm (vm context, worker): `instanceof Error` is false here */
function crossRealmError(): unknown {
  return runInNewContext(
    `(() => { const e = new TypeError(${JSON.stringify(`bad input "${PROMPT}"`)}); return e; })()`,
  );
}

describe("Bedrock error recording", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  beforeEach(() => {
    exporter.reset();
  });

  /** A client whose send() throws (or rejects with) the given value before any request */
  function failingClient(thrown: unknown, traceConfig: TraceConfigOptions = {}) {
    class BedrockRuntimeClient {
      send(_command: unknown): Promise<unknown> {
        if (thrown instanceof Promise) {
          return thrown;
        }
        throw thrown;
      }
    }
    const instrumentation = new BedrockInstrumentation({ tracerProvider: provider, traceConfig });
    instrumentation.manuallyInstrument({
      BedrockRuntimeClient,
    } as unknown as Parameters<BedrockInstrumentation["manuallyInstrument"]>[0]);
    return new BedrockRuntimeClient();
  }

  const commands: [string, () => InvokeModelCommand | ConverseCommand][] = [
    [
      "InvokeModel",
      () =>
        new InvokeModelCommand({
          modelId: "anthropic.claude-3-haiku-20240307-v1:0",
          body: JSON.stringify({ messages: [{ role: "user", content: PROMPT }] }),
        }),
    ],
    [
      "Converse",
      () =>
        new ConverseCommand({
          modelId: "anthropic.claude-3-haiku-20240307-v1:0",
          messages: [{ role: "user", content: [{ text: PROMPT }] }],
        }),
    ],
  ];

  async function onlySpanAfter(send: Promise<unknown> | (() => unknown)): Promise<ReadableSpan> {
    await expect(
      (async () => (typeof send === "function" ? send() : send))(),
    ).rejects.toBeDefined();
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    return spans[0];
  }

  it.each(commands)(
    "%s: a cross-realm error thrown before the request is message-only, with error.type",
    async (_name, command) => {
      const client = failingClient(crossRealmError());
      const span = await onlySpanAfter(() => client.send(command()));
      expect(span.status).toEqual({
        code: SpanStatusCode.ERROR,
        message: `bad input "${PROMPT}"`,
      });
      expect(span.attributes["error.type"]).toBe("TypeError");
      const event = span.events.find((e) => e.name === "exception");
      expect(event?.attributes?.["exception.type"]).toBe("TypeError");
    },
  );

  it.each(commands)("%s: a thrown string is the description as is", async (_name, command) => {
    const client = failingClient("throttled");
    const span = await onlySpanAfter(() => client.send(command()));
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: "throttled" });
    expect(span.attributes["error.type"]).toBe("_OTHER");
  });

  it.each(commands)("%s: a rejected plain object has no '[object Object]'", async (_name, command) => {
    const client = failingClient(Promise.reject({ name: "ThrottlingException", message: "slow down" }));
    const span = await onlySpanAfter(() => client.send(command()));
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: "slow down" });
    expect(span.attributes["error.type"]).toBe("ThrottlingException");
  });

  it.each(commands)("%s: hidden inputs hide the error text and the prompt", async (_name, command) => {
    const client = failingClient(crossRealmError(), { hideInputs: true });
    const span = await onlySpanAfter(() => client.send(command()));
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR });
    expect(span.attributes["error.type"]).toBe("TypeError");
    expect(
      JSON.stringify({ attributes: span.attributes, events: span.events, status: span.status }),
    ).not.toContain(PROMPT);
  });
});
