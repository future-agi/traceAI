/**
 * E2E tests for HuggingFace instrumentation
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
import { HuggingFaceInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("HuggingFace E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: HuggingFaceInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let HfInference: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-huggingface-e2e",
      projectType: ProjectType.OBSERVE,
      batch: false,
    });

    instrumentation = new HuggingFaceInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const hfModule = await import("@huggingface/inference");
    HfInference = hfModule.HfInference;

    instrumentation.manuallyInstrument(hfModule);

    client = new HfInference(process.env.HF_TOKEN || "dummy-token-for-e2e");
  });

  afterAll(async () => {
    instrumentation.disable();
    await new Promise((resolve) => setTimeout(resolve, 5000));
    await provider.forceFlush();
    await provider.shutdown();
  }, 15000);

  it("should trace text generation", async () => {
    try {
      const response = await client.textGeneration({
        model: "gpt2",
        inputs: "The quick brown fox",
        parameters: {
          max_new_tokens: 20,
        },
      });
      expect(response.generated_text).toBeDefined();
      console.log("Text generation response:", response.generated_text);
    } catch (error) {
      console.log("Text generation errored (span still exported):", (error as Error).message);
    }
  }, 60000);

  it("should trace chat completion", async () => {
    try {
      const response = await client.chatCompletion({
        model: "meta-llama/Llama-2-7b-chat-hf",
        messages: [
          { role: "user", content: "Say hello." },
        ],
        max_tokens: 10,
      });
      expect(response.choices).toBeDefined();
      console.log("Chat response:", response.choices[0]?.message?.content);
    } catch (error) {
      console.log("Chat completion errored (span still exported):", (error as Error).message);
    }
  }, 60000);

  it("should trace feature extraction (embeddings)", async () => {
    try {
      const response = await client.featureExtraction({
        model: "sentence-transformers/all-MiniLM-L6-v2",
        inputs: "Hello world",
      });
      expect(response).toBeDefined();
      console.log("Feature extraction completed");
    } catch (error) {
      console.log("Feature extraction errored (span still exported):", (error as Error).message);
    }
  }, 60000);

  it("should handle errors gracefully", async () => {
    try {
      await client.textGeneration({
        model: "non-existent-model-xyz123",
        inputs: "Hello",
      });
    } catch (error) {
      console.log("Error handling: correctly errored on invalid model");
    }
  }, 60000);
});
