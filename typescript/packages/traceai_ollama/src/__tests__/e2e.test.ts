/**
 * E2E tests for Ollama instrumentation
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
import { OllamaInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const OLLAMA_HOST = process.env.OLLAMA_HOST || "http://localhost:11434";
const OLLAMA_MODEL = process.env.OLLAMA_MODEL || "llama3.2";
const OLLAMA_EMBED_MODEL = process.env.OLLAMA_EMBED_MODEL || "nomic-embed-text";

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Ollama E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: OllamaInstrumentation;
  let Ollama: typeof import("ollama").Ollama;
  let client: InstanceType<typeof Ollama>;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-ollama-e2e",
      projectType: ProjectType.OBSERVE,
      batch: false,
    });

    instrumentation = new OllamaInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const ollamaModule = await import("ollama");
    instrumentation.manuallyInstrument(ollamaModule as unknown as Record<string, unknown>);
    Ollama = ollamaModule.Ollama;
    client = new Ollama({ host: OLLAMA_HOST });
  });

  afterAll(async () => {
    instrumentation.disable();
    await new Promise((resolve) => setTimeout(resolve, 5000));
    await provider.forceFlush();
    await provider.shutdown();
  }, 15000);

  describe("Chat", () => {
    it("should complete a basic chat request", async () => {
      try {
        const response = await client.chat({
          model: OLLAMA_MODEL,
          messages: [
            { role: "user", content: "What is 2 + 2? Answer with just the number." }
          ],
          stream: false,
        });
        console.log("Chat response:", response.message.content);
      } catch (error) {
        console.log("Chat errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle system messages", async () => {
      try {
        const response = await client.chat({
          model: OLLAMA_MODEL,
          messages: [
            { role: "system", content: "You are a helpful assistant. Always respond with exactly one word." },
            { role: "user", content: "Say hello" }
          ],
          stream: false,
        });
        console.log("System message response:", response.message.content);
      } catch (error) {
        console.log("System message errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle multi-turn conversations", async () => {
      try {
        const response = await client.chat({
          model: OLLAMA_MODEL,
          messages: [
            { role: "user", content: "My name is TestUser." },
            { role: "assistant", content: "Hello TestUser! Nice to meet you." },
            { role: "user", content: "What is my name?" }
          ],
          stream: false,
        });
        console.log("Multi-turn response:", response.message.content);
      } catch (error) {
        console.log("Multi-turn errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle streaming chat responses", async () => {
      try {
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

        console.log("Streaming response:", chunks.join(""));
      } catch (error) {
        console.log("Streaming errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle JSON format", async () => {
      try {
        const response = await client.chat({
          model: OLLAMA_MODEL,
          messages: [
            { role: "user", content: "Return a JSON object with a 'greeting' key set to 'hello'. Only output valid JSON, nothing else." }
          ],
          format: "json",
          stream: false,
        });
        console.log("JSON format response:", response.message.content);
      } catch (error) {
        console.log("JSON format errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle options like temperature", async () => {
      try {
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
        console.log("Options response:", response.message.content);
      } catch (error) {
        console.log("Options errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("Generate", () => {
    it("should complete a basic generate request", async () => {
      try {
        const response = await client.generate({
          model: OLLAMA_MODEL,
          prompt: "The capital of France is",
          stream: false,
        });
        console.log("Generate response:", response.response);
      } catch (error) {
        console.log("Generate errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle system prompt in generate", async () => {
      try {
        const response = await client.generate({
          model: OLLAMA_MODEL,
          prompt: "Say hello",
          system: "You are a helpful assistant. Always respond in French.",
          stream: false,
        });
        console.log("Generate system prompt response:", response.response);
      } catch (error) {
        console.log("Generate system prompt errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle streaming generate responses", async () => {
      try {
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

        console.log("Streaming generate response:", chunks.join(""));
      } catch (error) {
        console.log("Streaming generate errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle generation options", async () => {
      try {
        const response = await client.generate({
          model: OLLAMA_MODEL,
          prompt: "Write a haiku about coding.",
          options: {
            temperature: 0.7,
            num_predict: 100,
          },
          stream: false,
        });
        console.log("Generation options response:", response.response);
      } catch (error) {
        console.log("Generation options errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("Embeddings", () => {
    it("should generate embeddings with embed method", async () => {
      try {
        const response = await client.embed({
          model: OLLAMA_EMBED_MODEL,
          input: "Hello, world!",
        });
        console.log("Embedding dimensions:", response.embeddings[0]?.length);
      } catch (error) {
        console.log("Embedding errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should generate batch embeddings", async () => {
      try {
        const response = await client.embed({
          model: OLLAMA_EMBED_MODEL,
          input: ["Hello", "World", "Test"],
        });
        console.log("Batch embeddings count:", response.embeddings.length);
      } catch (error) {
        console.log("Batch embedding errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should generate embeddings with legacy embeddings method", async () => {
      try {
        const response = await client.embeddings({
          model: OLLAMA_EMBED_MODEL,
          prompt: "Hello, world!",
        });
        console.log("Legacy embedding dimensions:", response.embedding?.length);
      } catch (error) {
        console.log("Legacy embedding errored (span still exported):", (error as Error).message);
      }
    }, 30000);
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
      console.log("Error handling: correctly threw on invalid model");
    }, 30000);
  });

  describe("Token Usage", () => {
    it("should return token counts in chat response", async () => {
      try {
        const response = await client.chat({
          model: OLLAMA_MODEL,
          messages: [
            { role: "user", content: "Hello" }
          ],
          stream: false,
        });
        console.log("Token counts - eval:", response.eval_count, "prompt_eval:", response.prompt_eval_count);
      } catch (error) {
        console.log("Token count errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should return token counts in generate response", async () => {
      try {
        const response = await client.generate({
          model: OLLAMA_MODEL,
          prompt: "Hello",
          stream: false,
        });
        console.log("Token counts - eval:", response.eval_count, "prompt_eval:", response.prompt_eval_count);
      } catch (error) {
        console.log("Generate token count errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });
});
