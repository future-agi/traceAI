/**
 * E2E tests for Mistral instrumentation
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
import { MistralInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Mistral E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: MistralInstrumentation;
  let Mistral: typeof import("@mistralai/mistralai").Mistral;
  let client: InstanceType<typeof Mistral>;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-mistral-e2e",
      projectType: ProjectType.OBSERVE,
      batch: false,
    });

    instrumentation = new MistralInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const mistralModule = await import("@mistralai/mistralai");
    instrumentation.manuallyInstrument(mistralModule as unknown as Record<string, unknown>);
    Mistral = mistralModule.Mistral;
    client = new Mistral({ apiKey: process.env.MISTRAL_API_KEY || "dummy-key-for-e2e" });
  });

  afterAll(async () => {
    instrumentation.disable();
    await new Promise((resolve) => setTimeout(resolve, 5000));
    await provider.forceFlush();
    await provider.shutdown();
  }, 15000);

  describe("Chat Completions", () => {
    it("should complete a basic chat request", async () => {
      try {
        const response = await client.chat.complete({
          model: "mistral-small-latest",
          messages: [
            { role: "user", content: "What is 2 + 2? Answer with just the number." }
          ],
        });
        console.log("Chat response:", response.choices?.[0]?.message?.content);
      } catch (error) {
        console.log("Chat errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle system prompts", async () => {
      try {
        const response = await client.chat.complete({
          model: "mistral-small-latest",
          messages: [
            { role: "system", content: "You are a helpful assistant. Always respond with exactly one word." },
            { role: "user", content: "Say hello" }
          ],
        });
        console.log("System prompt response:", response.choices?.[0]?.message?.content);
      } catch (error) {
        console.log("System prompt errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle multi-turn conversations", async () => {
      try {
        const response = await client.chat.complete({
          model: "mistral-small-latest",
          messages: [
            { role: "user", content: "My name is TestUser." },
            { role: "assistant", content: "Hello TestUser!" },
            { role: "user", content: "What is my name?" }
          ],
        });
        console.log("Multi-turn response:", response.choices?.[0]?.message?.content);
      } catch (error) {
        console.log("Multi-turn errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle streaming responses", async () => {
      try {
        const stream = await client.chat.stream({
          model: "mistral-small-latest",
          messages: [
            { role: "user", content: "Count from 1 to 3." }
          ],
        });

        const chunks: string[] = [];
        for await (const chunk of stream) {
          const content = chunk.data?.choices?.[0]?.delta?.content;
          if (content && typeof content === 'string') {
            chunks.push(content);
          }
        }

        console.log("Streaming response:", chunks.join(""));
      } catch (error) {
        console.log("Streaming errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle JSON mode", async () => {
      try {
        const response = await client.chat.complete({
          model: "mistral-small-latest",
          messages: [
            { role: "user", content: "Return a JSON object with a 'greeting' key set to 'hello'" }
          ],
          responseFormat: { type: "json_object" },
        });
        console.log("JSON mode response:", response.choices?.[0]?.message?.content);
      } catch (error) {
        console.log("JSON mode errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle tool/function calling", async () => {
      try {
        const response = await client.chat.complete({
          model: "mistral-small-latest",
          messages: [
            { role: "user", content: "What is the weather in Tokyo?" }
          ],
          tools: [
            {
              type: "function",
              function: {
                name: "get_weather",
                description: "Get weather for a location",
                parameters: {
                  type: "object",
                  properties: {
                    location: { type: "string", description: "City name" }
                  },
                  required: ["location"]
                }
              }
            }
          ],
          toolChoice: "auto",
        });
        console.log("Tool calling response received");
      } catch (error) {
        console.log("Tool calling errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("Embeddings", () => {
    it("should generate embeddings for text", async () => {
      try {
        const response = await client.embeddings.create({
          model: "mistral-embed",
          inputs: ["Hello, world!"],
        });
        console.log("Embedding response received");
      } catch (error) {
        console.log("Embedding errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should generate embeddings for multiple texts", async () => {
      try {
        const response = await client.embeddings.create({
          model: "mistral-embed",
          inputs: ["Hello", "World", "Test"],
        });
        console.log("Batch embedding response received");
      } catch (error) {
        console.log("Batch embedding errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      await expect(
        client.chat.complete({
          model: "non-existent-model-12345",
          messages: [{ role: "user", content: "Hello" }],
        })
      ).rejects.toThrow();
      console.log("Error handling: correctly threw on invalid model");
    }, 30000);

    it("should handle empty messages gracefully", async () => {
      await expect(
        client.chat.complete({
          model: "mistral-small-latest",
          messages: [],
        })
      ).rejects.toThrow();
      console.log("Error handling: correctly threw on empty messages");
    }, 30000);
  });
});
