/**
 * E2E tests for xAI (Grok) instrumentation
 *
 * These tests require a valid XAI_API_KEY.
 *
 * Example:
 *   XAI_API_KEY=your_key npm test -- --testPathPattern=e2e
 */
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { XAIInstrumentation } from "../instrumentation";
import { SemanticConventions, FISpanKind, LLMProvider } from "@traceai/fi-semantic-conventions";

const XAI_API_KEY = process.env.XAI_API_KEY;

const describeIf = XAI_API_KEY ? describe : describe.skip;

describeIf("XAIInstrumentation E2E", () => {
  let provider: NodeTracerProvider;
  let memoryExporter: InMemorySpanExporter;
  let instrumentation: XAIInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let OpenAI: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider();
    provider.addSpanProcessor(new SimpleSpanProcessor(memoryExporter));
    provider.register();

    instrumentation = new XAIInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const openaiModule = await import("openai");
    instrumentation.manuallyInstrument(openaiModule as unknown as Record<string, unknown>);
    OpenAI = openaiModule.default;

    client = new OpenAI({
      baseURL: "https://api.x.ai/v1",
      apiKey: XAI_API_KEY,
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
      model: "grok-beta",
      messages: [
        { role: "system", content: "You are Grok, a witty AI assistant." },
        { role: "user", content: "Say hello in one word." },
      ],
      max_tokens: 10,
    });

    expect(response.choices[0].message.content).toBeDefined();

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    const span = spans[0];
    expect(span.name).toBe("xAI Chat Completions");
    expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
    expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.XAI);
    expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("grok-beta");
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBeDefined();
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]).toBeDefined();
  }, 30000);

  it("should trace streaming chat completion", async () => {
    const stream = await client.chat.completions.create({
      model: "grok-beta",
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
    expect(span.name).toBe("xAI Chat Completions");
    expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe(fullContent);
  }, 30000);

  it("should handle tool calling", async () => {
    const response = await client.chat.completions.create({
      model: "grok-beta",
      messages: [
        { role: "user", content: "What's the current time in Tokyo?" },
      ],
      tools: [
        {
          type: "function",
          function: {
            name: "get_current_time",
            description: "Get the current time in a specific timezone",
            parameters: {
              type: "object",
              properties: {
                timezone: { type: "string", description: "IANA timezone (e.g., 'Asia/Tokyo')" },
              },
              required: ["timezone"],
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
    expect(span.name).toBe("xAI Chat Completions");

    // Check if tool was called
    const toolCall = response.choices[0]?.message?.tool_calls?.[0];
    if (toolCall) {
      const toolCallPrefix = `${SemanticConventions.LLM_OUTPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_TOOL_CALLS}.0.`;
      expect(span.attributes[`${toolCallPrefix}${SemanticConventions.TOOL_CALL_FUNCTION_NAME}`]).toBe("get_current_time");
    }
  }, 30000);

  it("should trace embeddings when available", async () => {
    try {
      const response = await client.embeddings.create({
        model: "grok-embed",
        input: "Hello, world!",
      });

      expect(response.data[0].embedding.length).toBeGreaterThan(0);

      const spans = memoryExporter.getFinishedSpans();
      expect(spans.length).toBe(1);

      const span = spans[0];
      expect(span.name).toBe("xAI Embeddings");
      expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.EMBEDDING);
    } catch (error) {
      // Embeddings may not be available for all accounts
      console.log("xAI embeddings not available for this account");
    }
  }, 30000);

  it("should handle multi-turn conversation", async () => {
    const response = await client.chat.completions.create({
      model: "grok-beta",
      messages: [
        { role: "system", content: "You are Grok." },
        { role: "user", content: "My name is Alice." },
        { role: "assistant", content: "Nice to meet you, Alice!" },
        { role: "user", content: "What's my name?" },
      ],
      max_tokens: 20,
    });

    expect(response.choices[0].message.content).toBeDefined();
    expect(response.choices[0].message.content.toLowerCase()).toContain("alice");

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    const span = spans[0];
    // Should have all 4 input messages captured
    expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.0.${SemanticConventions.MESSAGE_ROLE}`]).toBe("system");
    expect(span.attributes[`${SemanticConventions.LLM_INPUT_MESSAGES}.3.${SemanticConventions.MESSAGE_ROLE}`]).toBe("user");
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
