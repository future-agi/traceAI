/**
 * E2E Tests for @traceai/llamaindex
 *
 * These tests run against LlamaIndex with an OpenAI-compatible backend
 * and export spans to the FI backend via register() from @traceai/fi-core.
 *
 * Required environment variables:
 *   FI_API_KEY     - FI platform API key
 *   FI_SECRET_KEY  - FI platform secret key (if required)
 *   GOOGLE_API_KEY - Google API key for OpenAI-compatible endpoint
 *
 * Run with: FI_API_KEY=... GOOGLE_API_KEY=... pnpm test -- --testPathPattern=e2e
 */
import { register, FITracerProvider, ProjectType } from "@traceai/fi-core";
import { LlamaIndexInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const GOOGLE_API_KEY = process.env.GOOGLE_API_KEY;

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("LlamaIndex E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: LlamaIndexInstrumentation;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-llamaindex-e2e",
      projectType: ProjectType.OBSERVE,
      batch: false,
    });

    instrumentation = new LlamaIndexInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const llamaindexModule = await import("llamaindex");
    instrumentation.manuallyInstrument(llamaindexModule as any);
  });

  afterAll(async () => {
    instrumentation.disable();
    await new Promise((resolve) => setTimeout(resolve, 5000));
    await provider.forceFlush();
    await provider.shutdown();
  }, 15000);

  describe("OpenAI LLM", () => {
    it("should complete a basic LLM request via OpenAI-compatible endpoint", async () => {
      const { OpenAI } = await import("llamaindex");

      const llm = new OpenAI({
        model: "gemini-2.0-flash",
        apiKey: GOOGLE_API_KEY,
        additionalSessionOptions: {
          baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
        },
        maxTokens: 20,
      });

      const response = await llm.complete({ prompt: "What is 2 + 2? Answer with just the number." });
      expect(response.text).toBeDefined();
      expect(response.text.length).toBeGreaterThan(0);
      console.log("LLM complete response:", response.text);
    }, 30000);

    it("should handle chat messages", async () => {
      const { OpenAI } = await import("llamaindex");

      const llm = new OpenAI({
        model: "gemini-2.0-flash",
        apiKey: GOOGLE_API_KEY,
        additionalSessionOptions: {
          baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
        },
        maxTokens: 50,
      });

      const response = await llm.chat({
        messages: [
          { role: "system", content: "You are a helpful assistant." },
          { role: "user", content: "Say hello in one word." },
        ],
      });

      expect(response.message.content).toBeDefined();
      console.log("Chat response:", response.message.content);
    }, 30000);
  });

  describe("Error Handling", () => {
    it("should handle invalid configuration gracefully", async () => {
      const { OpenAI } = await import("llamaindex");

      const llm = new OpenAI({
        model: "non-existent-model-12345",
        apiKey: GOOGLE_API_KEY,
        additionalSessionOptions: {
          baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
        },
      });

      await expect(
        llm.complete({ prompt: "Hello" })
      ).rejects.toThrow();
      console.log("Error handling: correctly threw on invalid model");
    }, 30000);
  });
});
