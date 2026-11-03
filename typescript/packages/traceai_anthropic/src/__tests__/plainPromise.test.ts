/**
 * A create() that returns a plain promise (an unknown SDK build or a test double), not an
 * SDK APIPromise. A result the attribute helpers cannot read must not break the caller.
 */
import { describe, it, expect, afterAll } from "@jest/globals";
import Anthropic from "@anthropic-ai/sdk";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { AnthropicInstrumentation } from "../instrumentation";

let nextResult: unknown = null;
class PlainSDK {
  static Messages = class {
    create() { return Promise.resolve(nextResult); }
  };
}

const memory = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
const instrumentation = new AnthropicInstrumentation({ instrumentationConfig: { enabled: false } });
instrumentation.setTracerProvider(provider);
instrumentation.enable();
instrumentation.manuallyInstrument(PlainSDK as unknown as typeof Anthropic);
const messages = new PlainSDK.Messages() as unknown as Anthropic.Messages;
const params = { model: "synthetic-model", max_tokens: 8, messages: [{ role: "user" as const, content: "hi" }] };

describe("plain-promise create()", () => {
  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  it.each([
    ["null", null],
    ["a non-object", "unexpected"],
  ])("returns %s unchanged and ends the span", async (_label, value) => {
    memory.reset();
    nextResult = value;
    await expect(messages.create(params)).resolves.toBe(value);
    const spans = memory.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.OK);
  });
});
