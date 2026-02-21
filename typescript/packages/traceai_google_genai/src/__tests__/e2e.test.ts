/**
 * E2E tests for Google GenAI instrumentation
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
import { SchemaType } from "@google/generative-ai";
import { GoogleGenAIInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Google GenAI E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: GoogleGenAIInstrumentation;
  let GoogleGenerativeAI: typeof import("@google/generative-ai").GoogleGenerativeAI;
  let genAI: InstanceType<typeof GoogleGenerativeAI>;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-google-genai-e2e",
      projectType: ProjectType.OBSERVE,
      batch: false,
    });

    instrumentation = new GoogleGenAIInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const googleModule = await import("@google/generative-ai");
    instrumentation.manuallyInstrument(googleModule as unknown as Record<string, unknown>);
    GoogleGenerativeAI = googleModule.GoogleGenerativeAI;
    genAI = new GoogleGenerativeAI(process.env.GOOGLE_API_KEY || "dummy-key-for-e2e");
  });

  afterAll(async () => {
    instrumentation.disable();
    await new Promise((resolve) => setTimeout(resolve, 5000));
    await provider.forceFlush();
    await provider.shutdown();
  }, 15000);

  describe("Content Generation", () => {
    it("should complete a basic content generation request", async () => {
      try {
        const model = genAI.getGenerativeModel({ model: "gemini-1.5-flash" });
        const result = await model.generateContent("What is 2 + 2? Answer with just the number.");
        const response = await result.response;
        console.log("Content generation response:", response.text());
      } catch (error) {
        console.log("Content generation errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle system instructions", async () => {
      try {
        const model = genAI.getGenerativeModel({
          model: "gemini-1.5-flash",
          systemInstruction: "You are a helpful assistant. Always respond with exactly one word.",
        });
        const result = await model.generateContent("Say hello");
        const response = await result.response;
        console.log("System instruction response:", response.text());
      } catch (error) {
        console.log("System instruction errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle multi-turn chat", async () => {
      try {
        const model = genAI.getGenerativeModel({ model: "gemini-1.5-flash" });
        const chat = model.startChat({
          history: [
            { role: "user", parts: [{ text: "My name is TestUser." }] },
            { role: "model", parts: [{ text: "Hello TestUser! Nice to meet you." }] },
          ],
        });

        const result = await chat.sendMessage("What is my name?");
        const response = await result.response;
        console.log("Multi-turn chat response:", response.text());
      } catch (error) {
        console.log("Multi-turn chat errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle streaming responses", async () => {
      try {
        const model = genAI.getGenerativeModel({ model: "gemini-1.5-flash" });
        const result = await model.generateContentStream("Count from 1 to 3.");

        const chunks: string[] = [];
        for await (const chunk of result.stream) {
          const text = chunk.text();
          if (text) {
            chunks.push(text);
          }
        }

        console.log("Streaming response:", chunks.join(""));
      } catch (error) {
        console.log("Streaming errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle JSON mode", async () => {
      try {
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
        console.log("JSON mode response:", response.text());
      } catch (error) {
        console.log("JSON mode errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle generation config", async () => {
      try {
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
        console.log("Generation config response:", response.text());
      } catch (error) {
        console.log("Generation config errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle function calling", async () => {
      try {
        const model = genAI.getGenerativeModel({
          model: "gemini-1.5-flash",
          tools: [
            {
              functionDeclarations: [
                {
                  name: "get_weather",
                  description: "Get weather for a location",
                  parameters: {
                    type: SchemaType.OBJECT,
                    properties: {
                      location: { type: SchemaType.STRING, description: "City name" },
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
        console.log("Function calling response received");
      } catch (error) {
        console.log("Function calling errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("Embeddings", () => {
    it("should generate embeddings for text", async () => {
      try {
        const model = genAI.getGenerativeModel({ model: "text-embedding-004" });
        const result = await model.embedContent("Hello, world!");
        console.log("Embedding dimensions:", result.embedding.values.length);
      } catch (error) {
        console.log("Embedding errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should generate batch embeddings", async () => {
      try {
        const model = genAI.getGenerativeModel({ model: "text-embedding-004" });
        const result = await model.batchEmbedContents({
          requests: [
            { content: { role: "user", parts: [{ text: "Hello" }] } },
            { content: { role: "user", parts: [{ text: "World" }] } },
            { content: { role: "user", parts: [{ text: "Test" }] } },
          ],
        });
        console.log("Batch embeddings count:", result.embeddings.length);
      } catch (error) {
        console.log("Batch embedding errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      const model = genAI.getGenerativeModel({ model: "non-existent-model-12345" });
      await expect(model.generateContent("Hello")).rejects.toThrow();
      console.log("Error handling: correctly threw on invalid model");
    }, 30000);
  });
});
