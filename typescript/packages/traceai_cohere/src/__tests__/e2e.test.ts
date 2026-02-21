/**
 * E2E tests for Cohere instrumentation
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
import { register, FITracerProvider } from "@traceai/fi-core";
import { CohereInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Cohere E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: CohereInstrumentation;
  let CohereClient: typeof import("cohere-ai").CohereClient;
  let client: InstanceType<typeof CohereClient>;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-cohere-e2e",
      batch: false,
    });

    instrumentation = new CohereInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const cohereModule = await import("cohere-ai");
    CohereClient = cohereModule.CohereClient;
    client = new CohereClient({ token: process.env.COHERE_API_KEY || "dummy-key-for-e2e" });
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  describe("Chat", () => {
    it("should complete a basic chat request", async () => {
      try {
        const response = await client.chat({
          message: "What is 2 + 2? Answer with just the number.",
          model: "command-r-08-2024",
        });
        console.log("Chat response:", response.text);
      } catch (error) {
        console.log("Chat errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle preamble (system prompt)", async () => {
      try {
        const response = await client.chat({
          message: "Say hello",
          model: "command-r-08-2024",
          preamble: "You are a helpful assistant. Always respond with exactly one word.",
        });
        console.log("Preamble response:", response.text);
      } catch (error) {
        console.log("Preamble errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle multi-turn conversations with history", async () => {
      try {
        const response = await client.chat({
          message: "What is my name?",
          model: "command-r-08-2024",
          chatHistory: [
            { role: "USER", message: "My name is TestUser." },
            { role: "CHATBOT", message: "Hello TestUser! Nice to meet you." },
          ],
        });
        console.log("Multi-turn response:", response.text);
      } catch (error) {
        console.log("Multi-turn errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle streaming responses", async () => {
      try {
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

        console.log("Streaming response:", chunks.join(""));
      } catch (error) {
        console.log("Streaming errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle tool calling", async () => {
      try {
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
        console.log("Tool calling response received");
      } catch (error) {
        console.log("Tool calling errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("Embeddings", () => {
    it("should generate embeddings for text", async () => {
      try {
        const response = await client.embed({
          texts: ["Hello, world!"],
          model: "embed-english-v3.0",
          inputType: "search_document",
        });
        console.log("Embedding response received");
      } catch (error) {
        console.log("Embedding errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should generate embeddings for multiple texts", async () => {
      try {
        const response = await client.embed({
          texts: ["Hello", "World", "Test"],
          model: "embed-english-v3.0",
          inputType: "search_document",
        });
        console.log("Batch embedding response received");
      } catch (error) {
        console.log("Batch embedding errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should generate multilingual embeddings", async () => {
      try {
        const response = await client.embed({
          texts: ["Hello", "Bonjour", "Hola", "Hallo"],
          model: "embed-multilingual-v3.0",
          inputType: "search_document",
        });
        console.log("Multilingual embedding response received");
      } catch (error) {
        console.log("Multilingual embedding errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("Rerank", () => {
    it("should rerank documents", async () => {
      try {
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
        console.log("Rerank response received, results:", response.results.length);
      } catch (error) {
        console.log("Rerank errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should rerank and return documents", async () => {
      try {
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
        console.log("Rerank with documents response received");
      } catch (error) {
        console.log("Rerank with documents errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle multilingual reranking", async () => {
      try {
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
        console.log("Multilingual rerank response received");
      } catch (error) {
        console.log("Multilingual rerank errored (span still exported):", (error as Error).message);
      }
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
      console.log("Error handling: correctly threw on invalid model");
    }, 30000);
  });
});
