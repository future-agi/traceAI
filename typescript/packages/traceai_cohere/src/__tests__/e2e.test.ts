/**
 * E2E Tests for @traceai/cohere
 *
 * These tests run against the real Cohere API.
 * Set COHERE_API_KEY environment variable to run these tests.
 *
 * Run with: COHERE_API_KEY=your_key pnpm test -- --testPathPattern=e2e
 */

import { CohereInstrumentation } from "../instrumentation";

// Skip all tests if API key is not set
const COHERE_API_KEY = process.env.COHERE_API_KEY;
const describeE2E = COHERE_API_KEY ? describe : describe.skip;

describeE2E("Cohere E2E Tests", () => {
  let instrumentation: CohereInstrumentation;
  let CohereClient: typeof import("cohere-ai").CohereClient;
  let client: InstanceType<typeof CohereClient>;

  beforeAll(async () => {
    // Initialize instrumentation
    instrumentation = new CohereInstrumentation();
    instrumentation.enable();

    // Import and initialize Cohere client
    const cohereModule = await import("cohere-ai");
    CohereClient = cohereModule.CohereClient;
    client = new CohereClient({ token: COHERE_API_KEY });
  });

  afterAll(() => {
    instrumentation.disable();
  });

  describe("Chat", () => {
    it("should complete a basic chat request", async () => {
      const response = await client.chat({
        message: "What is 2 + 2? Answer with just the number.",
        model: "command-r-08-2024",
      });

      expect(response.text).toBeDefined();
      expect(response.text.length).toBeGreaterThan(0);
    }, 30000);

    it("should handle preamble (system prompt)", async () => {
      const response = await client.chat({
        message: "Say hello",
        model: "command-r-08-2024",
        preamble: "You are a helpful assistant. Always respond with exactly one word.",
      });

      expect(response.text).toBeDefined();
    }, 30000);

    it("should handle multi-turn conversations with history", async () => {
      const response = await client.chat({
        message: "What is my name?",
        model: "command-r-08-2024",
        chatHistory: [
          { role: "USER", message: "My name is TestUser." },
          { role: "CHATBOT", message: "Hello TestUser! Nice to meet you." },
        ],
      });

      expect(response.text.toLowerCase()).toContain("testuser");
    }, 30000);

    it("should handle streaming responses", async () => {
      const stream = await client.chatStream({
        message: "Count from 1 to 3.",
        model: "command-r-08-2024",
      });

      const chunks: string[] = [];
      for await (const event of stream) {
        if (event.eventType === "text-generation") {
          chunks.push(event.text);
        }
      }

      expect(chunks.length).toBeGreaterThan(0);
      const fullResponse = chunks.join("");
      expect(fullResponse).toBeDefined();
    }, 30000);

    it("should handle tool calling", async () => {
      const response = await client.chat({
        message: "What is the weather in Tokyo?",
        model: "command-r-08-2024",
        tools: [
          {
            name: "get_weather",
            description: "Get weather for a location",
            parameterDefinitions: {
              location: {
                type: "str",
                description: "City name",
                required: true,
              },
            },
          },
        ],
      });

      // Should either have tool calls or a text response
      expect(response.toolCalls !== undefined || response.text !== undefined).toBe(true);
    }, 30000);
  });

  describe("Embeddings", () => {
    it("should generate embeddings for text", async () => {
      const response = await client.embed({
        texts: ["Hello, world!"],
        model: "embed-english-v3.0",
        inputType: "search_document",
      });

      expect(response.embeddings).toBeDefined();
      const embeddings = response.embeddings as number[][];
      expect(embeddings.length).toBe(1);
      expect(embeddings[0].length).toBeGreaterThan(0);
    }, 30000);

    it("should generate embeddings for multiple texts", async () => {
      const response = await client.embed({
        texts: ["Hello", "World", "Test"],
        model: "embed-english-v3.0",
        inputType: "search_document",
      });

      expect(response.embeddings).toBeDefined();
      const embeddings = response.embeddings as number[][];
      expect(embeddings.length).toBe(3);
    }, 30000);

    it("should generate multilingual embeddings", async () => {
      const response = await client.embed({
        texts: ["Hello", "Bonjour", "Hola", "Hallo"],
        model: "embed-multilingual-v3.0",
        inputType: "search_document",
      });

      expect(response.embeddings).toBeDefined();
      const embeddings = response.embeddings as number[][];
      expect(embeddings.length).toBe(4);
    }, 30000);
  });

  describe("Rerank", () => {
    it("should rerank documents", async () => {
      const response = await client.rerank({
        query: "What is the capital of France?",
        documents: [
          "Paris is the capital of France.",
          "Berlin is the capital of Germany.",
          "London is the capital of the United Kingdom.",
        ],
        model: "rerank-english-v3.0",
        topN: 2,
      });

      expect(response.results).toBeDefined();
      expect(response.results.length).toBe(2);
      // The document about Paris should be ranked first
      expect(response.results[0].index).toBe(0);
      expect(response.results[0].relevanceScore).toBeGreaterThan(0);
    }, 30000);

    it("should rerank and return documents", async () => {
      const response = await client.rerank({
        query: "How do computers work?",
        documents: [
          "Computers process data using electronic circuits.",
          "The Internet connects computers worldwide.",
          "Programming languages are used to write software.",
        ],
        model: "rerank-english-v3.0",
        topN: 2,
        returnDocuments: true,
      });

      expect(response.results).toBeDefined();
      expect(response.results.length).toBe(2);
      expect(response.results[0].document).toBeDefined();
      expect(response.results[0].document?.text).toBeDefined();
    }, 30000);

    it("should handle multilingual reranking", async () => {
      const response = await client.rerank({
        query: "What is artificial intelligence?",
        documents: [
          "AI is a branch of computer science.",
          "Machine learning is a subset of AI.",
          "Natural language processing enables computers to understand text.",
        ],
        model: "rerank-multilingual-v3.0",
        topN: 3,
      });

      expect(response.results).toBeDefined();
      expect(response.results.length).toBe(3);
    }, 30000);
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      await expect(
        client.chat({
          message: "Hello",
          model: "non-existent-model-12345" as any,
        })
      ).rejects.toThrow();
    }, 30000);
  });
});
