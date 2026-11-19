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
import { SemanticConventions } from "@traceai/fi-semantic-conventions";

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
    {
      label: "adjacent text blocks",
      content: [
        { type: "text", text: "first " },
        { type: "text", text: "second" },
      ],
      toolCalls: undefined,
    },
    {
      label: "text-tool-text blocks",
      content: [
        { type: "text", text: "first " },
        {
          type: "tool_use",
          id: "toolu_fake_weather",
          name: "get_weather",
          input: { location: "New York" },
        },
        { type: "text", text: "second" },
      ],
      toolCalls: [{ function: { name: "get_weather", arguments: '{"location":"New York"}' } }],
    },
    {
      label: "a trailing empty text block",
      content: [
        { type: "text", text: "first " },
        { type: "text", text: "second" },
        { type: "text", text: "" },
      ],
      toolCalls: undefined,
    },
  ])("preserves $label in canonical output", async ({ content, toolCalls }) => {
    memory.reset();
    const response: Anthropic.Message = {
      id: "msg_fake_multiblock",
      type: "message",
      role: "assistant",
      model: "synthetic-model",
      content: content as Anthropic.ContentBlock[],
      stop_reason: toolCalls ? "tool_use" : "end_turn",
      stop_sequence: null,
      usage: { input_tokens: 3, output_tokens: 8 },
    };
    const originalResponse = JSON.stringify(response);
    nextResult = response;

    const result = await messages.create(params);
    await provider.forceFlush();

    expect(result).toBe(response);
    expect(JSON.stringify(result)).toBe(originalResponse);
    const spans = memory.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.OK);
    const attributes = spans[0].attributes;
    expect(attributes[SemanticConventions.RAW_OUTPUT]).toBe(originalResponse);
    expect(attributes[SemanticConventions.OUTPUT_VALUE]).toBe(originalResponse);
    const outputMessages = JSON.parse(attributes[SemanticConventions.LLM_OUTPUT_MESSAGES] as string);
    expect(outputMessages).toHaveLength(1);
    expect(outputMessages[0].role).toBe("assistant");
    expect(outputMessages[0].content).toBe("first second");
    expect(outputMessages[0].tool_calls).toEqual(toolCalls);
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
