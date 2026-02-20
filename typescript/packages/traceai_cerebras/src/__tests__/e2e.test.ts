/**
 * E2E tests for Cerebras instrumentation
 *
 * These tests require a valid CEREBRAS_API_KEY.
 *
 * Example:
 *   CEREBRAS_API_KEY=your_key npm test -- --testPathPattern=e2e
 */
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { CerebrasInstrumentation } from "../instrumentation";
import { SemanticConventions, FISpanKind, LLMProvider } from "@traceai/fi-semantic-conventions";

const CEREBRAS_API_KEY = process.env.CEREBRAS_API_KEY;

const describeIf = CEREBRAS_API_KEY ? describe : describe.skip;

describeIf("CerebrasInstrumentation E2E", () => {
  let provider: NodeTracerProvider;
  let memoryExporter: InMemorySpanExporter;
  let instrumentation: CerebrasInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let Cerebras: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider();
    provider.addSpanProcessor(new SimpleSpanProcessor(memoryExporter));
    provider.register();

    instrumentation = new CerebrasInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const cerebrasModule = await import("@cerebras/cerebras_cloud_sdk");
    instrumentation.manuallyInstrument(cerebrasModule as unknown as Record<string, unknown>);
    Cerebras = cerebrasModule.default;

    client = new Cerebras({
      apiKey: CEREBRAS_API_KEY,
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
      model: "llama3.1-8b",
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
    expect(span.name).toBe("Cerebras Chat Completions");
    expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
    expect(span.attributes[SemanticConventions.LLM_PROVIDER]).toBe(LLMProvider.CEREBRAS);
    expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe("llama3.1-8b");
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBeDefined();
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]).toBeDefined();
  }, 30000);

  it("should capture time_info metrics", async () => {
    const response = await client.chat.completions.create({
      model: "llama3.1-8b",
      messages: [
        { role: "user", content: "What is 2+2?" },
      ],
      max_tokens: 10,
    });

    expect(response.choices[0].message.content).toBeDefined();

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    const span = spans[0];

    // Cerebras provides detailed time info
    if (response.time_info) {
      expect(span.attributes["cerebras.queue_time"]).toBeDefined();
      expect(span.attributes["cerebras.prompt_time"]).toBeDefined();
      expect(span.attributes["cerebras.completion_time"]).toBeDefined();
      expect(span.attributes["cerebras.total_time"]).toBeDefined();
    }
  }, 30000);

  it("should trace streaming chat completion", async () => {
    const stream = await client.chat.completions.create({
      model: "llama3.1-8b",
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
    expect(span.name).toBe("Cerebras Chat Completions");
    expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe(fullContent);
  }, 30000);

  it("should demonstrate fast inference", async () => {
    const startTime = Date.now();

    const response = await client.chat.completions.create({
      model: "llama3.1-70b",
      messages: [
        { role: "user", content: "What is the capital of France? Answer in one word." },
      ],
      max_tokens: 10,
    });

    const endTime = Date.now();
    const latency = endTime - startTime;

    expect(response.choices[0].message.content).toBeDefined();

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    // Cerebras is known for fast inference
    console.log(`Cerebras inference latency: ${latency}ms`);

    if (response.time_info) {
      console.log(`Server total time: ${response.time_info.total_time}s`);
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
