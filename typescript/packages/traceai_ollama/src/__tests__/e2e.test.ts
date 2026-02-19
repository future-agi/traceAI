/**
 * E2E Tests for @traceai/ollama
 *
 * These tests run against a real Ollama instance.
 * Ensure Ollama is running locally: ollama serve
 *
 * Default models used:
 * - llama3.2 (or llama2) for chat/generate
 * - nomic-embed-text for embeddings
 *
 * Pull models before running:
 * - ollama pull llama3.2
 * - ollama pull nomic-embed-text
 *
 * Run with: OLLAMA_HOST=http://localhost:11434 pnpm test -- --testPathPattern=e2e
 */

import { OllamaInstrumentation } from "../instrumentation";

// Check if Ollama is available
const OLLAMA_HOST = process.env.OLLAMA_HOST || "http://localhost:11434";
const OLLAMA_MODEL = process.env.OLLAMA_MODEL || "llama3.2";
const OLLAMA_EMBED_MODEL = process.env.OLLAMA_EMBED_MODEL || "nomic-embed-text";

// Helper to check if Ollama is running
async function isOllamaAvailable(): Promise<boolean> {
  try {
    const response = await fetch(`${OLLAMA_HOST}/api/tags`);
    return response.ok;
  } catch {
    return false;
  }
}

// Conditionally run tests
let describeE2E = describe.skip;

beforeAll(async () => {
  const available = await isOllamaAvailable();
  if (available) {
    describeE2E = describe;
  } else {
    console.log("Skipping Ollama E2E tests - Ollama not available at", OLLAMA_HOST);
  }
});

describeE2E("Ollama E2E Tests", () => {
  let instrumentation: OllamaInstrumentation;
  let Ollama: typeof import("ollama").Ollama;
  let client: InstanceType<typeof Ollama>;

  beforeAll(async () => {
    // Initialize instrumentation
    instrumentation = new OllamaInstrumentation();
    instrumentation.enable();

    // Import and initialize Ollama client
    const ollamaModule = await import("ollama");
    Ollama = ollamaModule.Ollama;
    client = new Ollama({ host: OLLAMA_HOST });
  });

  afterAll(() => {
    instrumentation.disable();
  });

  describe("Chat", () => {
    it("should complete a basic chat request", async () => {
      const response = await client.chat({
        model: OLLAMA_MODEL,
        messages: [
          { role: "user", content: "What is 2 + 2? Answer with just the number." }
        ],
        stream: false,
      });

      expect(response.message).toBeDefined();
      expect(response.message.content).toBeDefined();
      expect(response.message.content.length).toBeGreaterThan(0);
      expect(response.done).toBe(true);
    }, 60000);

    it("should handle system messages", async () => {
      const response = await client.chat({
        model: OLLAMA_MODEL,
        messages: [
          { role: "system", content: "You are a helpful assistant. Always respond with exactly one word." },
          { role: "user", content: "Say hello" }
        ],
        stream: false,
      });

      expect(response.message.content).toBeDefined();
    }, 60000);

    it("should handle multi-turn conversations", async () => {
      const response = await client.chat({
        model: OLLAMA_MODEL,
        messages: [
          { role: "user", content: "My name is TestUser." },
          { role: "assistant", content: "Hello TestUser! Nice to meet you." },
          { role: "user", content: "What is my name?" }
        ],
        stream: false,
      });

      expect(response.message.content.toLowerCase()).toContain("testuser");
    }, 60000);

    it("should handle streaming chat responses", async () => {
      const stream = await client.chat({
        model: OLLAMA_MODEL,
        messages: [
          { role: "user", content: "Count from 1 to 3." }
        ],
        stream: true,
      });

      const chunks: string[] = [];
      for await (const chunk of stream) {
        if (chunk.message?.content) {
          chunks.push(chunk.message.content);
        }
      }

      expect(chunks.length).toBeGreaterThan(0);
      const fullResponse = chunks.join("");
      expect(fullResponse).toBeDefined();
      expect(fullResponse.length).toBeGreaterThan(0);
    }, 60000);

    it("should handle JSON format", async () => {
      const response = await client.chat({
        model: OLLAMA_MODEL,
        messages: [
          { role: "user", content: "Return a JSON object with a 'greeting' key set to 'hello'. Only output valid JSON, nothing else." }
        ],
        format: "json",
        stream: false,
      });

      expect(response.message.content).toBeDefined();

      // Try to parse as JSON
      const parsed = JSON.parse(response.message.content);
      expect(parsed).toBeDefined();
    }, 60000);

    it("should handle options like temperature", async () => {
      const response = await client.chat({
        model: OLLAMA_MODEL,
        messages: [
          { role: "user", content: "Write a one-sentence story." }
        ],
        options: {
          temperature: 0.5,
          top_p: 0.9,
          num_predict: 50,
        },
        stream: false,
      });

      expect(response.message.content).toBeDefined();
    }, 60000);
  });

  describe("Generate", () => {
    it("should complete a basic generate request", async () => {
      const response = await client.generate({
        model: OLLAMA_MODEL,
        prompt: "The capital of France is",
        stream: false,
      });

      expect(response.response).toBeDefined();
      expect(response.response.length).toBeGreaterThan(0);
      expect(response.done).toBe(true);
    }, 60000);

    it("should handle system prompt in generate", async () => {
      const response = await client.generate({
        model: OLLAMA_MODEL,
        prompt: "Say hello",
        system: "You are a helpful assistant. Always respond in French.",
        stream: false,
      });

      expect(response.response).toBeDefined();
    }, 60000);

    it("should handle streaming generate responses", async () => {
      const stream = await client.generate({
        model: OLLAMA_MODEL,
        prompt: "Count from 1 to 3.",
        stream: true,
      });

      const chunks: string[] = [];
      for await (const chunk of stream) {
        if (chunk.response) {
          chunks.push(chunk.response);
        }
      }

      expect(chunks.length).toBeGreaterThan(0);
      const fullResponse = chunks.join("");
      expect(fullResponse).toBeDefined();
    }, 60000);

    it("should handle generation options", async () => {
      const response = await client.generate({
        model: OLLAMA_MODEL,
        prompt: "Write a haiku about coding.",
        options: {
          temperature: 0.7,
          num_predict: 100,
        },
        stream: false,
      });

      expect(response.response).toBeDefined();
    }, 60000);
  });

  describe("Embeddings", () => {
    it("should generate embeddings with embed method", async () => {
      const response = await client.embed({
        model: OLLAMA_EMBED_MODEL,
        input: "Hello, world!",
      });

      expect(response.embeddings).toBeDefined();
      expect(response.embeddings.length).toBe(1);
      expect(response.embeddings[0].length).toBeGreaterThan(0);
    }, 60000);

    it("should generate batch embeddings", async () => {
      const response = await client.embed({
        model: OLLAMA_EMBED_MODEL,
        input: ["Hello", "World", "Test"],
      });

      expect(response.embeddings).toBeDefined();
      expect(response.embeddings.length).toBe(3);
      response.embeddings.forEach((embedding) => {
        expect(embedding.length).toBeGreaterThan(0);
      });
    }, 60000);

    it("should generate embeddings with legacy embeddings method", async () => {
      const response = await client.embeddings({
        model: OLLAMA_EMBED_MODEL,
        prompt: "Hello, world!",
      });

      expect(response.embedding).toBeDefined();
      expect(response.embedding.length).toBeGreaterThan(0);
    }, 60000);
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      await expect(
        client.chat({
          model: "non-existent-model-12345",
          messages: [{ role: "user", content: "Hello" }],
          stream: false,
        })
      ).rejects.toThrow();
    }, 30000);
  });

  describe("Token Usage", () => {
    it("should return token counts in chat response", async () => {
      const response = await client.chat({
        model: OLLAMA_MODEL,
        messages: [
          { role: "user", content: "Hello" }
        ],
        stream: false,
      });

      // Ollama returns eval_count (completion tokens) and prompt_eval_count (prompt tokens)
      expect(response.eval_count).toBeDefined();
      expect(response.prompt_eval_count).toBeDefined();
    }, 60000);

    it("should return token counts in generate response", async () => {
      const response = await client.generate({
        model: OLLAMA_MODEL,
        prompt: "Hello",
        stream: false,
      });

      expect(response.eval_count).toBeDefined();
      expect(response.prompt_eval_count).toBeDefined();
    }, 60000);
  });
});
