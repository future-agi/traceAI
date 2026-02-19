/**
 * E2E tests for vLLM instrumentation
 *
 * These tests require a running vLLM server.
 * Set VLLM_BASE_URL environment variable to your vLLM server URL.
 *
 * Example:
 *   VLLM_BASE_URL=http://localhost:8000/v1 npm test -- --testPathPattern=e2e
 */
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { VLLMInstrumentation } from "../instrumentation";
import { SemanticConventions, FISpanKind, LLMSystem } from "@traceai/fi-semantic-conventions";

const VLLM_BASE_URL = process.env.VLLM_BASE_URL;
const VLLM_MODEL = process.env.VLLM_MODEL || "meta-llama/Llama-2-7b-chat-hf";

const describeIf = VLLM_BASE_URL ? describe : describe.skip;

describeIf("VLLMInstrumentation E2E", () => {
  let provider: NodeTracerProvider;
  let memoryExporter: InMemorySpanExporter;
  let instrumentation: VLLMInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let OpenAI: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    memoryExporter = new InMemorySpanExporter();
    provider = new NodeTracerProvider();
    provider.addSpanProcessor(new SimpleSpanProcessor(memoryExporter));
    provider.register();

    instrumentation = new VLLMInstrumentation({
      baseUrlPattern: "localhost",
    });
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const openaiModule = await import("openai");
    instrumentation.manuallyInstrument(openaiModule as unknown as Record<string, unknown>);
    OpenAI = openaiModule.default;

    client = new OpenAI({
      baseURL: VLLM_BASE_URL,
      apiKey: "not-needed",
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
      model: VLLM_MODEL,
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
    expect(span.name).toBe("vLLM Chat Completions");
    expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
    expect(span.attributes[SemanticConventions.LLM_SYSTEM]).toBe(LLMSystem.VLLM);
    expect(span.attributes[SemanticConventions.LLM_MODEL_NAME]).toBe(VLLM_MODEL);
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_PROMPT]).toBeDefined();
    expect(span.attributes[SemanticConventions.LLM_TOKEN_COUNT_COMPLETION]).toBeDefined();
  }, 30000);

  it("should trace streaming chat completion", async () => {
    const stream = await client.chat.completions.create({
      model: VLLM_MODEL,
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
    expect(span.name).toBe("vLLM Chat Completions");
    expect(span.attributes[SemanticConventions.OUTPUT_VALUE]).toBe(fullContent);
  }, 30000);

  it("should trace text completion", async () => {
    const response = await client.completions.create({
      model: VLLM_MODEL,
      prompt: "The capital of France is",
      max_tokens: 10,
    });

    expect(response.choices[0].text).toBeDefined();

    const spans = memoryExporter.getFinishedSpans();
    expect(spans.length).toBe(1);

    const span = spans[0];
    expect(span.name).toBe("vLLM Completions");
    expect(span.attributes[SemanticConventions.FI_SPAN_KIND]).toBe(FISpanKind.LLM);
    expect(span.attributes[SemanticConventions.LLM_SYSTEM]).toBe(LLMSystem.VLLM);
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
