import { expect, it, jest } from "@jest/globals";
import Anthropic from "@anthropic-ai/sdk";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { AnthropicInstrumentation } from "../instrumentation";

it("keeps the original Messages instrumented after instrumenting a Messages subclass", async () => {
  class DerivedSDK extends Anthropic {
    static Messages = class DerivedMessages extends Anthropic.Messages {};
  }
  const params = {
    model: "synthetic-model",
    max_tokens: 8,
    messages: [{ role: "user" as const, content: "subclass fixture" }],
  };
  const response = {
    id: "msg_subclass_fixture",
    type: "message",
    role: "assistant",
    model: params.model,
    content: [{ type: "text", text: "subclass response" }],
    stop_reason: "end_turn",
    stop_sequence: null,
    usage: { input_tokens: 3, output_tokens: 2 },
  };
  const sdkCreate = jest.spyOn(Anthropic.Messages.prototype, "create");
  const syntheticFetch = jest.fn(async () => new Response(JSON.stringify(response), {
    status: 200,
    headers: { "content-type": "application/json" },
  }));
  const client = new Anthropic({
    apiKey: "synthetic",
    baseURL: "https://offline.invalid",
    maxRetries: 0,
    fetch: syntheticFetch as any,
  });
  const memory = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(memory)] });
  const instrumentation = new AnthropicInstrumentation({ instrumentationConfig: { enabled: false } });
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();

  async function expectOneSpan(messages: Anthropic.Messages) {
    memory.reset();
    sdkCreate.mockClear();
    syntheticFetch.mockClear();
    await expect(messages.create(params)).resolves.toEqual(response);
    expect(sdkCreate).toHaveBeenCalledTimes(1);
    expect(syntheticFetch).toHaveBeenCalledTimes(1);
    expect(memory.getFinishedSpans()).toHaveLength(1);
  }

  try {
    instrumentation.manuallyInstrument(Anthropic);
    await expectOneSpan(client.messages);

    // The subclass inherits create (and its wrapper), rather than defining its own.
    instrumentation.manuallyInstrument(DerivedSDK);
    await expectOneSpan(client.messages);
    await expectOneSpan(new DerivedSDK.Messages(client));

    instrumentation.manuallyInstrument(Anthropic);
    await expectOneSpan(client.messages);
  } finally {
    instrumentation.disable();
    sdkCreate.mockRestore();
    await provider.shutdown();
  }
});
