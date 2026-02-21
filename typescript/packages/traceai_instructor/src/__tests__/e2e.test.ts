/**
 * E2E Tests for @traceai/instructor
 *
 * These tests run against the Instructor library and export spans to the FI backend.
 * Set FI_API_KEY and GOOGLE_API_KEY environment variables to run these tests.
 *
 * Run with: FI_API_KEY=... GOOGLE_API_KEY=your_key pnpm test -- --testPathPattern=e2e
 */

import { register, FITracerProvider } from "@traceai/fi-core";
import { InstructorInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const GOOGLE_API_KEY = process.env.GOOGLE_API_KEY;
const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Instructor E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: InstructorInstrumentation;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-instructor-e2e",
      batch: false,
    });

    instrumentation = new InstructorInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  describe("Structured Output", () => {
    it("should extract structured data from text", async () => {
      const Instructor = await import("@instructor-ai/instructor");
      const OpenAI = (await import("openai")).default;
      const { z } = await import("zod");

      const openai = new OpenAI({
        baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
        apiKey: GOOGLE_API_KEY,
      });

      const instructor = Instructor.default({
        client: openai,
        mode: "JSON",
      });

      const UserSchema = z.object({
        name: z.string(),
        age: z.number(),
      });

      const result = await instructor.chat.completions.create({
        model: "gemini-2.0-flash",
        messages: [
          { role: "user", content: "John is 25 years old." },
        ],
        response_model: {
          schema: UserSchema,
          name: "User",
        },
        max_tokens: 100,
      });

      expect(result).toBeDefined();
      expect(result.name).toBeDefined();
      expect(result.age).toBeDefined();
    }, 30000);
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      const Instructor = await import("@instructor-ai/instructor");
      const OpenAI = (await import("openai")).default;
      const { z } = await import("zod");

      const openai = new OpenAI({
        baseURL: "https://generativelanguage.googleapis.com/v1beta/openai/",
        apiKey: GOOGLE_API_KEY,
      });

      const instructor = Instructor.default({
        client: openai,
        mode: "JSON",
      });

      const Schema = z.object({ value: z.string() });

      await expect(
        instructor.chat.completions.create({
          model: "non-existent-model-12345",
          messages: [{ role: "user", content: "Hello" }],
          response_model: { schema: Schema, name: "Test" },
        })
      ).rejects.toThrow();
    }, 30000);
  });
});
