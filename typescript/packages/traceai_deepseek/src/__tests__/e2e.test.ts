/**
 * E2E tests for DeepSeek instrumentation
 *
 * These tests require a valid DEEPSEEK_API_KEY.
 *
 * Example:
 *   DEEPSEEK_API_KEY=your_key npm test -- --testPathPattern=e2e
 */
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { DeepSeekInstrumentation } from "../instrumentation";
import { SemanticConventions, FISpanKind, LLMProvider } from "@traceai/fi-semantic-conventions";

const DEEPSEEK_API_KEY = process.env.DEEPSEEK_API_KEY;

const describeIf = DEEPSEEK_API_KEY ? describe : describe.skip;

describeIf("DeepSeekInstrumentation E2E", () => {
  let provider: NodeTracerProvider;
  let memoryExporter: InMemorySpanExporter;
  let instrumentation: DeepSeekInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let OpenAI: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider();
    provider.addSpanProcessor(new SimpleSpanProcessor(memoryExporter));
    provider.register();

    instrumentation = new DeepSeekInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const openaiModule = await import("openai");
    instrumentation.manuallyInstrument(openaiModule as unknown as Record<string, unknown>);
    OpenAI = openaiModule.default;

    client = new OpenAI({
      baseURL: "https://api.deepseek.com/v1",
      apiKey: DEEPSEEK_API_KEY,
    });
  });

  afterAll(async () => {
    await provider.shutdown();
  });

  beforeEach(() => {
    memoryExporter.reset();
  });

  it("should trace chat completion", async () => {
    const response = await client.chat.completions.create({
      model: "deepseek-chat",
      messages: [
        { role: "system", content: "You are a helpful assistant." },
        { role: "user", content: "Say hello in one word." },
      ],
      max_tokens: 10,
    });

    expect(response.choices[0].message.content).toBeDefined();

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    const span = spans[0];
    expect(span.name).toBe("DeepSeek Chat Completions");
    expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
    expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.DEEPSEEK);
    expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("deepseek-chat");
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBeDefined();
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]).toBeDefined();
  }, 30000);

  it("should trace streaming chat completion", async () => {
    const stream = await client.chat.completions.create({
      model: "deepseek-chat",
      messages: [
        { role: "user", content: "Count from 1 to 3." },
      ],
      max_tokens: 20,
      stream: true,
    });

    let fullContent = "";
    for await (const chunk of stream) {
      const content = chunk.choices[0]?.delta?.content;
      if (content) {
        fullContent += content;
      }
    }

    expect(fullContent.length).toBeGreaterThan(0);

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    const span = spans[0];
    expect(span.name).toBe("DeepSeek Chat Completions");
    expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe(fullContent);
  }, 30000);

  it("should capture cache metrics when available", async () => {
    // Make the same request twice to potentially hit cache
    const body = {
      model: "deepseek-chat",
      messages: [
        { role: "user", content: "What is 2+2?" },
      ],
      max_tokens: 10,
    };

    await client.chat.completions.create(body);
    memoryExporter.reset();
    await client.chat.completions.create(body);

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    // Cache metrics may or may not be present depending on DeepSeek's caching
    const span = spans[0];
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBeDefined();
  }, 60000);

  it("should handle tool calling", async () => {
    const response = await client.chat.completions.create({
      model: "deepseek-chat",
      messages: [
        { role: "user", content: "What's the weather in Paris?" },
      ],
      tools: [
        {
          type: "function",
          function: {
            name: "get_weather",
            description: "Get the current weather in a location",
            parameters: {
              type: "object",
              properties: {
                location: { type: "string", description: "City name" },
              },
              required: ["location"],
            },
          },
        },
      ],
      tool_choice: "auto",
      max_tokens: 100,
    });

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    const span = spans[0];
    expect(span.name).toBe("DeepSeek Chat Completions");

    // Check if tool was called
    const toolCall = response.choices[0]?.message?.tool_calls?.[0];
    if (toolCall) {
      const toolCallPrefix = `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_TOOL_CALLS}.0.`;
      expect(span.attributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_NAME}`]).toBe("get_weather");
    }
  }, 30000);

  it("should handle errors gracefully", async () => {
    await expect(
      client.chat.completions.create({
        model: "non-existent-model",
        messages: [{ role: "user", content: "Hello" }],
      })
    ).rejects.toThrow();

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);
    expect(spans[0].status.code).toBe(2); // ERROR
  }, 30000);
});
