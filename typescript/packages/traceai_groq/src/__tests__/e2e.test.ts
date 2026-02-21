/**
 * E2E tests for Groq instrumentation
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
import { GroqInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Groq E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: GroqInstrumentation;
  let Groq: typeof import("groq-sdk").default;
  let client: InstanceType<typeof Groq>;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-groq-e2e",
      projectType: ProjectType.OBSERVE,
      batch: false,
    });

    instrumentation = new GroqInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const groqModule = await import("groq-sdk");
    instrumentation.manuallyInstrument(groqModule as unknown as Record<string, unknown>);
    Groq = groqModule.default;
    client = new Groq({ apiKey: process.env.GROQ_API_KEY || "dummy-key-for-e2e" });
  });

  afterAll(async () => {
    instrumentation.disable();
    // Wait for SimpleSpanProcessor async HTTP exports to complete
    await new Promise((resolve) => setTimeout(resolve, 5000));
    await provider.forceFlush();
    await provider.shutdown();
  }, 15000);

  describe("Chat Completions", () => {
    it("should complete a basic chat request", async () => {
      try {
        const response = await client.chat.completions.create({
          model: "llama-3.1-8b-instant",
          messages: [
            { role: "user", content: "What is 2 + 2? Answer with just the number." }
          ],
          max_tokens: 10,
        });
        console.log("Chat response:", response.choices[0].message.content);
      } catch (error) {
        console.log("Chat errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle system prompts", async () => {
      try {
        const response = await client.chat.completions.create({
          model: "llama-3.1-8b-instant",
          messages: [
            { role: "system", content: "You are a helpful assistant. Always respond with exactly one word." },
            { role: "user", content: "Say hello" }
          ],
          max_tokens: 10,
        });
        console.log("System prompt response:", response.choices[0].message.content);
      } catch (error) {
        console.log("System prompt errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle multi-turn conversations", async () => {
      try {
        const response = await client.chat.completions.create({
          model: "llama-3.1-8b-instant",
          messages: [
            { role: "user", content: "My name is TestUser." },
            { role: "assistant", content: "Hello TestUser!" },
            { role: "user", content: "What is my name?" }
          ],
          max_tokens: 50,
        });
        console.log("Multi-turn response:", response.choices[0].message.content);
      } catch (error) {
        console.log("Multi-turn errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle streaming responses", async () => {
      try {
        const stream = await client.chat.completions.create({
          model: "llama-3.1-8b-instant",
          messages: [
            { role: "user", content: "Count from 1 to 3." }
          ],
          max_tokens: 50,
          stream: true,
        });

        const chunks: string[] = [];
        for await (const chunk of stream) {
          const content = chunk.choices[0]?.delta?.content;
          if (content) {
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
        const response = await client.chat.completions.create({
          model: "llama-3.1-8b-instant",
          messages: [
            { role: "system", content: "Respond only with valid JSON." },
            { role: "user", content: "Return a JSON object with a 'greeting' key set to 'hello'" }
          ],
          max_tokens: 100,
          response_format: { type: "json_object" },
        });
        console.log("JSON mode response:", response.choices[0].message.content);
      } catch (error) {
        console.log("JSON mode errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle tool/function calling", async () => {
      try {
        const response = await client.chat.completions.create({
          model: "llama-3.3-70b-versatile",
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
          tool_choice: "auto",
          max_tokens: 200,
        });
        console.log("Tool calling response received");
      } catch (error) {
        console.log("Tool calling errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      await expect(
        client.chat.completions.create({
          model: "non-existent-model-12345",
          messages: [{ role: "user", content: "Hello" }],
        })
      ).rejects.toThrow();
      console.log("Error handling: correctly threw on invalid model");
    }, 30000);

    it("should handle empty messages gracefully", async () => {
      await expect(
        client.chat.completions.create({
          model: "llama-3.1-8b-instant",
          messages: [],
        })
      ).rejects.toThrow();
      console.log("Error handling: correctly threw on empty messages");
    }, 30000);
  });
});
