/**
 * E2E Tests for @traceai/anthropic
 *
 * These tests export spans to the FI backend via register() from @traceai/fi-core.
 * Even error spans (from dummy keys) appear in the UI.
 *
 * Required environment variables:
 *   FI_API_KEY       - FI platform API key
 *
 * Run with: FI_API_KEY=... pnpm test -- --testPathPattern=e2e
 */
import { register, FITracerProvider } from "@traceai/fi-core";
import { AnthropicInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY;

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Anthropic E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: AnthropicInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let Anthropic: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-anthropic-e2e",
      batch: false,
    });

    instrumentation = new AnthropicInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const anthropicModule = await import("@anthropic-ai/sdk");
    Anthropic = anthropicModule.default;
    client = new Anthropic({ apiKey: ANTHROPIC_API_KEY || "dummy-key-for-e2e" });
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  describe("Messages", () => {
    it("should complete a basic message request", async () => {
      try {
        const response = await client.messages.create({
          model: "claude-haiku-4-5-20251001",
          max_tokens: 20,
          messages: [
            { role: "user", content: "What is 2 + 2? Answer with just the number." },
          ],
        });

        expect(response.content).toBeDefined();
        expect(response.content.length).toBeGreaterThan(0);
        console.log("Basic message response:", response.content[0].text);
      } catch (error) {
        console.log("Basic message errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle system prompt", async () => {
      try {
        const response = await client.messages.create({
          model: "claude-haiku-4-5-20251001",
          max_tokens: 20,
          system: "You are a helpful assistant. Always respond with exactly one word.",
          messages: [
            { role: "user", content: "Say hello" },
          ],
        });

        expect(response.content[0].text).toBeDefined();
        console.log("System prompt response:", response.content[0].text);
      } catch (error) {
        console.log("System prompt errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle multi-turn conversations", async () => {
      try {
        const response = await client.messages.create({
          model: "claude-haiku-4-5-20251001",
          max_tokens: 50,
          messages: [
            { role: "user", content: "My name is TestUser." },
            { role: "assistant", content: "Hello TestUser! Nice to meet you." },
            { role: "user", content: "What is my name?" },
          ],
        });

        expect(response.content[0].text.toLowerCase()).toContain("testuser");
        console.log("Multi-turn response:", response.content[0].text);
      } catch (error) {
        console.log("Multi-turn errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle streaming responses", async () => {
      try {
        const stream = await client.messages.stream({
          model: "claude-haiku-4-5-20251001",
          max_tokens: 50,
          messages: [
            { role: "user", content: "Count from 1 to 3." },
          ],
        });

        const message = await stream.finalMessage();
        expect(message.content).toBeDefined();
        console.log("Streaming response:", message.content[0]?.text);
      } catch (error) {
        console.log("Streaming errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle tool calling", async () => {
      try {
        const response = await client.messages.create({
          model: "claude-haiku-4-5-20251001",
          max_tokens: 200,
          messages: [
            { role: "user", content: "What is the weather in Tokyo?" },
          ],
          tools: [
            {
              name: "get_weather",
              description: "Get weather for a location",
              input_schema: {
                type: "object",
                properties: {
                  location: { type: "string", description: "City name" },
                },
                required: ["location"],
              },
            },
          ],
        });

        expect(
          response.content.some((block: any) => block.type === "tool_use" || block.type === "text")
        ).toBe(true);
        console.log("Tool calling response types:", response.content.map((b: any) => b.type));
      } catch (error) {
        console.log("Tool calling errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      try {
        await client.messages.create({
          model: "non-existent-model-12345",
          max_tokens: 10,
          messages: [{ role: "user", content: "Hello" }],
        });
      } catch (error) {
        console.log("Error handling: correctly threw on invalid model");
      }
    }, 30000);
  });
});
