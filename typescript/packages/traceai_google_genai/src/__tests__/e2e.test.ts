/**
 * E2E Tests for @traceai/google-genai
 *
 * These tests run against the real Google Generative AI API.
 * Set GOOGLE_API_KEY environment variable to run these tests.
 *
 * Run with: GOOGLE_API_KEY=your_key pnpm test -- --testPathPattern=e2e
 */

import { GoogleGenAIInstrumentation } from "../instrumentation";

// Skip all tests if API key is not set
const GOOGLE_API_KEY = process.env.GOOGLE_API_KEY;
const describeE2E = GOOGLE_API_KEY ? describe : describe.skip;

describeE2E("Google GenAI E2E Tests", () => {
  let instrumentation: GoogleGenAIInstrumentation;
  let GoogleGenerativeAI: typeof import("@google/generative-ai").GoogleGenerativeAI;
  let genAI: InstanceType<typeof GoogleGenerativeAI>;

  beforeAll(async () => {
    // Initialize instrumentation
    instrumentation = new GoogleGenAIInstrumentation();
    instrumentation.enable();

    // Import and initialize Google GenAI client
    const googleModule = await import("@google/generative-ai");
    GoogleGenerativeAI = googleModule.GoogleGenerativeAI;
    genAI = new GoogleGenerativeAI(GOOGLE_API_KEY!);
  });

  afterAll(() => {
    instrumentation.disable();
  });

  describe("Content Generation", () => {
    it("should complete a basic content generation request", async () => {
      const model = genAI.getGenerativeModel({ model: "gemini-1.5-flash" });
      const result = await model.generateContent("What is 2 + 2? Answer with just the number.");
      const response = await result.response;

      expect(response.text()).toBeDefined();
      expect(response.text().length).toBeGreaterThan(0);
    }, 30000);

    it("should handle system instructions", async () => {
      const model = genAI.getGenerativeModel({
        model: "gemini-1.5-flash",
        systemInstruction: "You are a helpful assistant. Always respond with exactly one word.",
      });
      const result = await model.generateContent("Say hello");
      const response = await result.response;

      expect(response.text()).toBeDefined();
    }, 30000);

    it("should handle multi-turn chat", async () => {
      const model = genAI.getGenerativeModel({ model: "gemini-1.5-flash" });
      const chat = model.startChat({
        history: [
          { role: "user", parts: [{ text: "My name is TestUser." }] },
          { role: "model", parts: [{ text: "Hello TestUser! Nice to meet you." }] },
        ],
      });

      const result = await chat.sendMessage("What is my name?");
      const response = await result.response;

      expect(response.text().toLowerCase()).toContain("testuser");
    }, 30000);

    it("should handle streaming responses", async () => {
      const model = genAI.getGenerativeModel({ model: "gemini-1.5-flash" });
      const result = await model.generateContentStream("Count from 1 to 3.");

      const chunks: string[] = [];
      for await (const chunk of result.stream) {
        const text = chunk.text();
        if (text) {
          chunks.push(text);
        }
      }

      expect(chunks.length).toBeGreaterThan(0);
      const fullResponse = chunks.join("");
      expect(fullResponse).toBeDefined();
    }, 30000);

    it("should handle JSON mode", async () => {
      const model = genAI.getGenerativeModel({
        model: "gemini-1.5-flash",
        generationConfig: {
          responseMimeType: "application/json",
        },
      });

      const result = await model.generateContent(
        "Return a JSON object with a 'greeting' key set to 'hello'"
      );
      const response = await result.response;
      const content = response.text();

      expect(content).toBeDefined();

      // Verify it's valid JSON
      const parsed = JSON.parse(content);
      expect(parsed).toBeDefined();
    }, 30000);

    it("should handle generation config", async () => {
      const model = genAI.getGenerativeModel({
        model: "gemini-1.5-flash",
        generationConfig: {
          temperature: 0.5,
          topP: 0.9,
          topK: 40,
          maxOutputTokens: 100,
        },
      });

      const result = await model.generateContent("Write a one-sentence story.");
      const response = await result.response;

      expect(response.text()).toBeDefined();
    }, 30000);

    it("should handle function calling", async () => {
      const model = genAI.getGenerativeModel({
        model: "gemini-1.5-flash",
        tools: [
          {
            functionDeclarations: [
              {
                name: "get_weather",
                description: "Get weather for a location",
                parameters: {
                  type: "object",
                  properties: {
                    location: { type: "string", description: "City name" },
                  },
                  required: ["location"],
                },
              },
            ],
          },
        ],
      });

      const result = await model.generateContent("What is the weather in Tokyo?");
      const response = await result.response;

      // Should either have function calls or a text response
      const functionCalls = response.functionCalls();
      expect(functionCalls !== undefined || response.text() !== undefined).toBe(true);
    }, 30000);
  });

  describe("Embeddings", () => {
    it("should generate embeddings for text", async () => {
      const model = genAI.getGenerativeModel({ model: "text-embedding-004" });
      const result = await model.embedContent("Hello, world!");

      expect(result.embedding).toBeDefined();
      expect(result.embedding.values).toBeDefined();
      expect(result.embedding.values.length).toBeGreaterThan(0);
    }, 30000);

    it("should generate batch embeddings", async () => {
      const model = genAI.getGenerativeModel({ model: "text-embedding-004" });
      const result = await model.batchEmbedContents({
        requests: [
          { content: { role: "user", parts: [{ text: "Hello" }] } },
          { content: { role: "user", parts: [{ text: "World" }] } },
          { content: { role: "user", parts: [{ text: "Test" }] } },
        ],
      });

      expect(result.embeddings).toBeDefined();
      expect(result.embeddings.length).toBe(3);
      result.embeddings.forEach((embedding) => {
        expect(embedding.values).toBeDefined();
        expect(embedding.values.length).toBeGreaterThan(0);
      });
    }, 30000);
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      const model = genAI.getGenerativeModel({ model: "non-existent-model-12345" });

      await expect(model.generateContent("Hello")).rejects.toThrow();
    }, 30000);
  });
});
