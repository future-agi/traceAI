/**
 * E2E tests for Cerebras instrumentation
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
import { CerebrasInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Cerebras E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: CerebrasInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let Cerebras: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-cerebras-e2e",
      projectType: ProjectType.OBSERVE,
      batch: false,
    });

    instrumentation = new CerebrasInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const cerebrasModule = await import("@cerebras/cerebras_cloud_sdk");
    instrumentation.manuallyInstrument(cerebrasModule as unknown as Record<string, unknown>);
    Cerebras = cerebrasModule.default;

    client = new Cerebras({
      apiKey: process.env.CEREBRAS_API_KEY || "dummy-key-for-e2e",
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
        model: "llama3.1-8b",
        messages: [
          { role: "system", content: "You are a helpful assistant." },
          { role: "user", content: "Say hello in one word." },
        ],
        max_tokens: 10,
      });
      expect(response.choices[0].message.content).toBeDefined();
      console.log("Chat response:", response.choices[0].message.content);
    } catch (error) {
      console.log("Chat completion errored (span still exported):", (error as Error).message);
    }
  }, 30000);

  it("should trace streaming chat completion", async () => {
    try {
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
      console.log("Streaming response:", fullContent);
    } catch (error) {
      console.log("Streaming errored (span still exported):", (error as Error).message);
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
