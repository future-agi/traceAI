/**
 * E2E Tests for @traceai/mistral
 *
 * These tests run against the real Mistral API.
 * Set MISTRAL_API_KEY environment variable to run these tests.
 *
 * Run with: MISTRAL_API_KEY=your_key pnpm test -- --testPathPattern=e2e
 */

import { MistralInstrumentation } from "../instrumentation";

// Skip all tests if API key is not set
const MISTRAL_API_KEY = process.env.MISTRAL_API_KEY;
const describeE2E = MISTRAL_API_KEY ? describe : describe.skip;

describeE2E("Mistral E2E Tests", () => {
  let instrumentation: MistralInstrumentation;
  let Mistral: typeof import("@mistralai/mistralai").Mistral;
  let client: InstanceType<typeof Mistral>;

  beforeAll(async () => {
    // Initialize instrumentation
    instrumentation = new MistralInstrumentation();
    instrumentation.enable();

    // Import and initialize Mistral client
    const mistralModule = await import("@mistralai/mistralai");
    Mistral = mistralModule.Mistral;
    client = new Mistral({ apiKey: MISTRAL_API_KEY });
  });

  afterAll(() => {
    instrumentation.disable();
  });

  describe("Chat Completions", () => {
    it("should complete a basic chat request", async () => {
      const response = await client.chat.complete({
        model: "mistral-small-latest",
        messages: [
          { role: "user", content: "What is 2 + 2? Answer with just the number." }
        ],
      });

      expect(response.choices).toBeDefined();
      expect(response.choices!.length).toBeGreaterThan(0);
      expect(response.choices![0].message?.content).toBeDefined();
      expect(response.usage).toBeDefined();
    }, 30000);

    it("should handle system prompts", async () => {
      const response = await client.chat.complete({
        model: "mistral-small-latest",
        messages: [
          { role: "system", content: "You are a helpful assistant. Always respond with exactly one word." },
          { role: "user", content: "Say hello" }
        ],
      });

      expect(response.choices![0].message?.content).toBeDefined();
    }, 30000);

    it("should handle multi-turn conversations", async () => {
      const response = await client.chat.complete({
        model: "mistral-small-latest",
        messages: [
          { role: "user", content: "My name is TestUser." },
          { role: "assistant", content: "Hello TestUser!" },
          { role: "user", content: "What is my name?" }
        ],
      });

      const content = response.choices![0].message?.content;
      expect(typeof content === "string" && content.toLowerCase().includes("testuser")).toBe(true);
    }, 30000);

    it("should handle streaming responses", async () => {
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

      expect(chunks.length).toBeGreaterThan(0);
      const fullResponse = chunks.join("");
      expect(fullResponse).toBeDefined();
    }, 30000);

    it("should handle JSON mode", async () => {
      const response = await client.chat.complete({
        model: "mistral-small-latest",
        messages: [
          { role: "user", content: "Return a JSON object with a 'greeting' key set to 'hello'" }
        ],
        responseFormat: { type: "json_object" },
      });

      const content = response.choices![0].message?.content;
      expect(content).toBeDefined();

      // Verify it's valid JSON
      const parsed = JSON.parse(content as string);
      expect(parsed).toBeDefined();
    }, 30000);

    it("should handle tool/function calling", async () => {
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

      // Should either have tool calls or a text response
      expect(
        response.choices![0].message?.toolCalls !== undefined ||
        response.choices![0].message?.content !== undefined
      ).toBe(true);
    }, 30000);
  });

  describe("Embeddings", () => {
    it("should generate embeddings for text", async () => {
      const response = await client.embeddings.create({
        model: "mistral-embed",
        inputs: ["Hello, world!"],
      });

      expect(response.data).toBeDefined();
      expect(response.data!.length).toBe(1);
      expect(response.data![0].embedding).toBeDefined();
      expect(response.data![0].embedding!.length).toBeGreaterThan(0);
    }, 30000);

    it("should generate embeddings for multiple texts", async () => {
      const response = await client.embeddings.create({
        model: "mistral-embed",
        inputs: ["Hello", "World", "Test"],
      });

      expect(response.data).toBeDefined();
      expect(response.data!.length).toBe(3);
      response.data!.forEach((item) => {
        expect(item.embedding).toBeDefined();
        expect(item.embedding!.length).toBeGreaterThan(0);
      });
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
    }, 30000);

    it("should handle empty messages gracefully", async () => {
      await expect(
        client.chat.complete({
          model: "mistral-small-latest",
          messages: [],
        })
      ).rejects.toThrow();
    }, 30000);
  });
});
