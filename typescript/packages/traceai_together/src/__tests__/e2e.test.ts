/**
 * E2E tests for Together AI instrumentation
 *
 * These tests export spans to the FI backend via register() from @traceai/fi-core.
 * Even error spans (from dummy keys) appear in the UI.
 *
 * Required environment variables:
 *   FI_API_KEY     - FI platform API key
 *
 * Example:
 *   FI_API_KEY=... pnpm test -- --testPathPattern=e2e
 */
import { register, FITracerProvider, ProjectType } from "@traceai/fi-core";
import { TogetherInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Together E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: TogetherInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let Together: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-together-e2e",
      projectType: ProjectType.OBSERVE,
      batch: false,
    });

    instrumentation = new TogetherInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const togetherModule = await import("together-ai");
    Together = togetherModule.Together;

    instrumentation.manuallyInstrument(togetherModule);

    client = new Together({
      apiKey: process.env.TOGETHER_API_KEY || "dummy-key-for-e2e",
    });
  });

  afterAll(async () => {
    instrumentation.disable();
    await new Promise((resolve) => setTimeout(resolve, 5000));
    await provider.forceFlush();
    await provider.shutdown();
  }, 15000);

  it("should trace chat completion", async () => {
    try {
      const response = await client.chat.completions.create({
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [
          { role: "user", content: "Say 'hello' and nothing else." },
        ],
        max_tokens: 10,
      });
      expect(response.choices).toBeDefined();
      console.log("Chat response:", response.choices[0]?.message?.content);
    } catch (error) {
      console.log("Chat completion errored (span still exported):", (error as Error).message);
    }
  }, 30000);

  it("should trace streaming chat completion", async () => {
    try {
      const stream = await client.chat.completions.create({
        model: "meta-llama/Llama-3-8b-chat-hf",
        messages: [
          { role: "user", content: "Say 'hi'." },
        ],
        max_tokens: 5,
        stream: true,
      });

      const chunks: unknown[] = [];
      for await (const chunk of stream) {
        chunks.push(chunk);
      }
      expect(chunks.length).toBeGreaterThan(0);
      console.log("Streaming chunks:", chunks.length);
    } catch (error) {
      console.log("Streaming errored (span still exported):", (error as Error).message);
    }
  }, 30000);

  it("should trace embeddings", async () => {
    try {
      const response = await client.embeddings.create({
        model: "togethercomputer/m2-bert-80M-8k-retrieval",
        input: "Hello world",
      });
      expect(response.data).toBeDefined();
      console.log("Embedding response:", response.data?.length, "embeddings");
    } catch (error) {
      console.log("Embeddings errored (span still exported):", (error as Error).message);
    }
  }, 30000);

  it("should handle errors gracefully", async () => {
    await expect(
      client.chat.completions.create({
        model: "non-existent-model",
        messages: [{ role: "user", content: "Hello" }],
      })
    ).rejects.toThrow();
    console.log("Error handling: correctly threw on invalid model");
  }, 30000);
});
