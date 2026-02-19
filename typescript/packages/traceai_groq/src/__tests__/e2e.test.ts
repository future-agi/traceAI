/**
 * E2E Tests for @traceai/groq
 *
 * These tests run against the real Groq API.
 * Set GROQ_API_KEY environment variable to run these tests.
 *
 * Run with: GROQ_API_KEY=your_key pnpm test -- --testPathPattern=e2e
 */

import { GroqInstrumentation } from "../instrumentation";

// Skip all tests if API key is not set
const GROQ_API_KEY = process.env.GROQ_API_KEY;
const describeE2E = GROQ_API_KEY ? describe : describe.skip;

describeE2E("Groq E2E Tests", () => {
  let instrumentation: GroqInstrumentation;
  let Groq: typeof import("groq-sdk").default;
  let client: InstanceType<typeof Groq>;

  beforeAll(async () => {
    // Initialize instrumentation
    instrumentation = new GroqInstrumentation();
    instrumentation.enable();

    // Import and initialize Groq client
    const groqModule = await import("groq-sdk");
    Groq = groqModule.default;
    client = new Groq({ apiKey: GROQ_API_KEY });
  });

  afterAll(() => {
    instrumentation.disable();
  });

  describe("Chat Completions", () => {
    it("should complete a basic chat request", async () => {
      const response = await client.chat.completions.create({
        model: "llama-3.1-8b-instant",
        messages: [
          { role: "user", content: "What is 2 + 2? Answer with just the number." }
        ],
        max_tokens: 10,
      });

      expect(response.choices).toBeDefined();
      expect(response.choices.length).toBeGreaterThan(0);
      expect(response.choices[0].message.content).toBeDefined();
      expect(response.usage).toBeDefined();
    }, 30000);

    it("should handle system prompts", async () => {
      const response = await client.chat.completions.create({
        model: "llama-3.1-8b-instant",
        messages: [
          { role: "system", content: "You are a helpful assistant. Always respond with exactly one word." },
          { role: "user", content: "Say hello" }
        ],
        max_tokens: 10,
      });

      expect(response.choices[0].message.content).toBeDefined();
    }, 30000);

    it("should handle multi-turn conversations", async () => {
      const response = await client.chat.completions.create({
        model: "llama-3.1-8b-instant",
        messages: [
          { role: "user", content: "My name is TestUser." },
          { role: "assistant", content: "Hello TestUser!" },
          { role: "user", content: "What is my name?" }
        ],
        max_tokens: 50,
      });

      expect(response.choices[0].message.content?.toLowerCase()).toContain("testuser");
    }, 30000);

    it("should handle streaming responses", async () => {
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

      expect(chunks.length).toBeGreaterThan(0);
      const fullResponse = chunks.join("");
      expect(fullResponse).toBeDefined();
    }, 30000);

    it("should handle JSON mode", async () => {
      const response = await client.chat.completions.create({
        model: "llama-3.1-8b-instant",
        messages: [
          { role: "system", content: "Respond only with valid JSON." },
          { role: "user", content: "Return a JSON object with a 'greeting' key set to 'hello'" }
        ],
        max_tokens: 100,
        response_format: { type: "json_object" },
      });

      const content = response.choices[0].message.content;
      expect(content).toBeDefined();

      // Verify it's valid JSON
      const parsed = JSON.parse(content!);
      expect(parsed).toBeDefined();
    }, 30000);

    it("should handle tool/function calling", async () => {
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

      // Should either have tool calls or a text response
      expect(
        response.choices[0].message.tool_calls !== undefined ||
        response.choices[0].message.content !== undefined
      ).toBe(true);
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
    }, 30000);

    it("should handle empty messages gracefully", async () => {
      await expect(
        client.chat.completions.create({
          model: "llama-3.1-8b-instant",
          messages: [],
        })
      ).rejects.toThrow();
    }, 30000);
  });
});
