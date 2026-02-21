/**
 * E2E Tests for @traceai/langchain
 *
 * These tests run against LangChain with an OpenAI-compatible backend
 * and export spans to the FI backend via register() from @traceai/fi-core.
 *
 * Required environment variables:
 *   FI_API_KEY     - FI platform API key
 *   FI_SECRET_KEY  - FI platform secret key (if required)
 *   GOOGLE_API_KEY - Google API key for OpenAI-compatible endpoint
 *
 * Run with: FI_API_KEY=... GOOGLE_API_KEY=... pnpm test -- --testPathPattern=e2e
 */
import { register, FITracerProvider } from "@traceai/fi-core";
import { LangChainInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const GOOGLE_API_KEY = process.env.GOOGLE_API_KEY;

const describeE2E = FI_API_KEY && GOOGLE_API_KEY ? describe : describe.skip;

describeE2E("LangChain E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: LangChainInstrumentation;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-langchain-e2e",
      batch: false,
    });

    instrumentation = new LangChainInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  describe("ChatOpenAI", () => {
    it("should complete a basic chat request via OpenAI-compatible endpoint", async () => {
      const { ChatOpenAI } = await import("@langchain/openai");

      const model = new ChatOpenAI({
        modelName: "gemini-2.0-flash",
        openAIApiKey: GOOGLE_API_KEY,
        configuration: {
          baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
        },
        maxTokens: 20,
      });

      const response = await model.invoke("What is 2 + 2? Answer with just the number.");
      expect(response.content).toBeDefined();
      expect(String(response.content).length).toBeGreaterThan(0);
      console.log("Chat response:", response.content);
    }, 30000);

    it("should handle multi-turn conversations", async () => {
      const { ChatOpenAI } = await import("@langchain/openai");
      const { HumanMessage, AIMessage, SystemMessage } = await import("@langchain/core/messages");

      const model = new ChatOpenAI({
        modelName: "gemini-2.0-flash",
        openAIApiKey: GOOGLE_API_KEY,
        configuration: {
          baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
        },
        maxTokens: 50,
      });

      const response = await model.invoke([
        new SystemMessage("You are a helpful assistant."),
        new HumanMessage("My name is TestUser."),
        new AIMessage("Hello TestUser! Nice to meet you."),
        new HumanMessage("What is my name?"),
      ]);

      expect(String(response.content).toLowerCase()).toContain("testuser");
      console.log("Multi-turn response:", response.content);
    }, 30000);

    it("should handle streaming responses", async () => {
      const { ChatOpenAI } = await import("@langchain/openai");

      const model = new ChatOpenAI({
        modelName: "gemini-2.0-flash",
        openAIApiKey: GOOGLE_API_KEY,
        configuration: {
          baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
        },
        maxTokens: 50,
        streaming: true,
      });

      const chunks: string[] = [];
      const stream = await model.stream("Count from 1 to 3.");
      for await (const chunk of stream) {
        if (chunk.content) {
          chunks.push(String(chunk.content));
        }
      }

      expect(chunks.length).toBeGreaterThan(0);
      console.log("Streaming response:", chunks.join(""));
    }, 30000);
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      const { ChatOpenAI } = await import("@langchain/openai");

      const model = new ChatOpenAI({
        modelName: "non-existent-model-12345",
        openAIApiKey: GOOGLE_API_KEY,
        configuration: {
          baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
        },
      });

      await expect(model.invoke("Hello")).rejects.toThrow();
      console.log("Error handling: correctly threw on invalid model");
    }, 30000);
  });
});
