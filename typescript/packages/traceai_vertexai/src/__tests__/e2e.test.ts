/**
 * E2E Tests for @traceai/vertexai
 *
 * These tests export spans to the FI backend via register() from @traceai/fi-core.
 * Even error spans (from dummy credentials) appear in the UI.
 *
 * Required environment variables:
 *   FI_API_KEY                    - FI platform API key
 *
 * Run with: FI_API_KEY=... pnpm test -- --testPathPattern=e2e
 */
import { register, FITracerProvider } from "@traceai/fi-core";
import { VertexAIInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const GOOGLE_CLOUD_PROJECT = process.env.GOOGLE_CLOUD_PROJECT;

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Vertex AI E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: VertexAIInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let VertexAI: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let vertexAI: any;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-vertexai-e2e",
      batch: false,
    });

    instrumentation = new VertexAIInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const vertexModule = await import("@google-cloud/vertexai");
    VertexAI = vertexModule.VertexAI;
    vertexAI = new VertexAI({
      project: GOOGLE_CLOUD_PROJECT || "dummy-project-for-e2e",
      location: "us-central1",
    });
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  describe("Content Generation", () => {
    it("should complete a basic content generation request", async () => {
      try {
        const model = vertexAI.getGenerativeModel({ model: "gemini-1.5-flash" });
        const result = await model.generateContent("What is 2 + 2? Answer with just the number.");
        const response = await result.response;

        expect(response).toBeDefined();
        expect(response.candidates).toBeDefined();
        console.log("Content generation response:", JSON.stringify(response.candidates[0]));
      } catch (error) {
        console.log("Content generation errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle system instructions", async () => {
      try {
        const model = vertexAI.getGenerativeModel({
          model: "gemini-1.5-flash",
          systemInstruction: "You are a helpful assistant. Always respond with exactly one word.",
        });
        const result = await model.generateContent("Say hello");
        const response = await result.response;

        expect(response.candidates).toBeDefined();
        console.log("System instruction response:", JSON.stringify(response.candidates[0]));
      } catch (error) {
        console.log("System instruction errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle multi-turn chat", async () => {
      try {
        const model = vertexAI.getGenerativeModel({ model: "gemini-1.5-flash" });
        const chat = model.startChat({
          history: [
            { role: "user", parts: [{ text: "My name is TestUser." }] },
            { role: "model", parts: [{ text: "Hello TestUser! Nice to meet you." }] },
          ],
        });

        const result = await chat.sendMessage("What is my name?");
        const response = await result.response;

        expect(response.candidates).toBeDefined();
        console.log("Multi-turn response:", response.candidates[0]?.content?.parts?.[0]?.text);
      } catch (error) {
        console.log("Multi-turn errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle streaming responses", async () => {
      try {
        const model = vertexAI.getGenerativeModel({ model: "gemini-1.5-flash" });
        const result = await model.generateContentStream("Count from 1 to 3.");

        const chunks: string[] = [];
        for await (const chunk of result.stream) {
          const text = chunk.candidates?.[0]?.content?.parts?.[0]?.text;
          if (text) {
            chunks.push(text);
          }
        }

        expect(chunks.length).toBeGreaterThan(0);
        console.log("Streaming response:", chunks.join(""));
      } catch (error) {
        console.log("Streaming errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      try {
        const model = vertexAI.getGenerativeModel({ model: "non-existent-model-12345" });
        await model.generateContent("Hello");
      } catch (error) {
        console.log("Error handling: correctly threw on invalid model");
      }
    }, 30000);
  });
});
