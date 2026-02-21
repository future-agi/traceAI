/**
 * E2E Tests for @traceai/vercel
 *
 * These tests verify the Vercel AI SDK instrumentation with FI span export.
 * Set FI_API_KEY and GOOGLE_API_KEY environment variables to run these tests.
 *
 * Run with: FI_API_KEY=... GOOGLE_API_KEY=your_key pnpm test -- --testPathPattern=e2e
 */

import { register, FITracerProvider } from "@traceai/fi-core";
import { InMemorySpanExporter } from "@opentelemetry/sdk-trace-base";
import { FISimpleSpanProcessor, isFISpan } from "../FISpanProcessor";

const FI_API_KEY = process.env.FI_API_KEY;
const GOOGLE_API_KEY = process.env.GOOGLE_API_KEY;
const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Vercel AI SDK E2E Tests", () => {
  let provider: FITracerProvider;

  beforeAll(() => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-vercel-e2e",
      batch: false,
    });
  });

  afterAll(async () => {
    await provider.shutdown();
  });

  describe("generateText", () => {
    it("should generate text using Vercel AI SDK with OpenAI provider", async () => {
      const { generateText } = await import("ai");
      const { createOpenAI } = await import("@ai-sdk/openai");

      const openai = createOpenAI({
        baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
        apiKey: GOOGLE_API_KEY!,
      });

      const result = await generateText({
        model: openai("gemini-2.0-flash"),
        prompt: "What is 2 + 2? Answer with just the number.",
        maxTokens: 10,
      });

      expect(result.text).toBeDefined();
      expect(result.text.length).toBeGreaterThan(0);
    }, 30000);

    it("should generate text with system prompt", async () => {
      const { generateText } = await import("ai");
      const { createOpenAI } = await import("@ai-sdk/openai");

      const openai = createOpenAI({
        baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
        apiKey: GOOGLE_API_KEY!,
      });

      const result = await generateText({
        model: openai("gemini-2.0-flash"),
        system: "You are a helpful assistant. Always respond with exactly one word.",
        prompt: "Say hello",
        maxTokens: 10,
      });

      expect(result.text).toBeDefined();
    }, 30000);
  });

  describe("streamText", () => {
    it("should stream text using Vercel AI SDK", async () => {
      const { streamText } = await import("ai");
      const { createOpenAI } = await import("@ai-sdk/openai");

      const openai = createOpenAI({
        baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
        apiKey: GOOGLE_API_KEY!,
      });

      const result = streamText({
        model: openai("gemini-2.0-flash"),
        prompt: "Count from 1 to 3.",
        maxTokens: 50,
      });

      const chunks: string[] = [];
      for await (const textPart of result.textStream) {
        chunks.push(textPart);
      }

      expect(chunks.length).toBeGreaterThan(0);
      expect(chunks.join("").length).toBeGreaterThan(0);
    }, 30000);
  });

  describe("SpanProcessor", () => {
    it("should export isFISpan utility", () => {
      expect(typeof isFISpan).toBe("function");
    });

    it("should create FISimpleSpanProcessor instance", () => {
      const exporter = new InMemorySpanExporter();
      const processor = new FISimpleSpanProcessor({ exporter });
      expect(processor).toBeDefined();
    });
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      const { generateText } = await import("ai");
      const { createOpenAI } = await import("@ai-sdk/openai");

      const openai = createOpenAI({
        baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
        apiKey: GOOGLE_API_KEY!,
      });

      await expect(
        generateText({
          model: openai("non-existent-model-12345"),
          prompt: "Hello",
        })
      ).rejects.toThrow();
    }, 30000);
  });
});
