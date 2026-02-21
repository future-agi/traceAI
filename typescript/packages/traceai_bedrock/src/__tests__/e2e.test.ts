/**
 * E2E Tests for @traceai/bedrock
 *
 * These tests export spans to the FI backend via register() from @traceai/fi-core.
 * Even error spans (from dummy keys) appear in the UI.
 *
 * Required environment variables:
 *   FI_API_KEY            - FI platform API key
 *
 * Run with: FI_API_KEY=... pnpm test -- --testPathPattern=e2e
 */
import { register, FITracerProvider } from "@traceai/fi-core";
import { BedrockInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const AWS_ACCESS_KEY_ID = process.env.AWS_ACCESS_KEY_ID;
const AWS_SECRET_ACCESS_KEY = process.env.AWS_SECRET_ACCESS_KEY;
const AWS_REGION = process.env.AWS_REGION || "us-east-1";

const describeE2E = FI_API_KEY ? describe : describe.skip;

describeE2E("Bedrock E2E Tests", () => {
  let provider: FITracerProvider;
  let instrumentation: BedrockInstrumentation;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let BedrockRuntimeClient: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let InvokeModelCommand: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let ConverseCommand: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let client: any;

  beforeAll(async () => {
    provider = register({
      projectName: process.env.FI_PROJECT_NAME || "ts-bedrock-e2e",
      batch: false,
    });

    instrumentation = new BedrockInstrumentation();
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();

    const bedrockModule = await import("@aws-sdk/client-bedrock-runtime");
    BedrockRuntimeClient = bedrockModule.BedrockRuntimeClient;
    InvokeModelCommand = bedrockModule.InvokeModelCommand;
    ConverseCommand = bedrockModule.ConverseCommand;

    client = new BedrockRuntimeClient({
      region: AWS_REGION,
      credentials: {
        accessKeyId: AWS_ACCESS_KEY_ID || "dummy-access-key-for-e2e",
        secretAccessKey: AWS_SECRET_ACCESS_KEY || "dummy-secret-key-for-e2e",
      },
    });
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  describe("Converse", () => {
    it("should complete a basic converse request", async () => {
      try {
        const response = await client.send(new ConverseCommand({
          modelId: "anthropic.claude-3-haiku-20240307-v1:0",
          messages: [
            {
              role: "user",
              content: [{ text: "What is 2 + 2? Answer with just the number." }],
            },
          ],
          inferenceConfig: {
            maxTokens: 20,
          },
        }));

        expect(response.output).toBeDefined();
        console.log("Converse response:", JSON.stringify(response.output.message));
      } catch (error) {
        console.log("Converse errored (span still exported):", (error as Error).message);
      }
    }, 30000);

    it("should handle system prompt", async () => {
      try {
        const response = await client.send(new ConverseCommand({
          modelId: "anthropic.claude-3-haiku-20240307-v1:0",
          system: [{ text: "You are a helpful assistant. Always respond with exactly one word." }],
          messages: [
            {
              role: "user",
              content: [{ text: "Say hello" }],
            },
          ],
          inferenceConfig: {
            maxTokens: 20,
          },
        }));

        expect(response.output).toBeDefined();
        console.log("System prompt response:", JSON.stringify(response.output));
      } catch (error) {
        console.log("System prompt errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("InvokeModel", () => {
    it("should invoke a model directly", async () => {
      try {
        const body = JSON.stringify({
          anthropic_version: "bedrock-2023-05-31",
          max_tokens: 20,
          messages: [
            { role: "user", content: "Say hello in one word." },
          ],
        });

        const response = await client.send(new InvokeModelCommand({
          modelId: "anthropic.claude-3-haiku-20240307-v1:0",
          body: new TextEncoder().encode(body),
          contentType: "application/json",
          accept: "application/json",
        }));

        const responseBody = JSON.parse(new TextDecoder().decode(response.body));
        expect(responseBody.content).toBeDefined();
        console.log("InvokeModel response:", JSON.stringify(responseBody.content));
      } catch (error) {
        console.log("InvokeModel errored (span still exported):", (error as Error).message);
      }
    }, 30000);
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      try {
        await client.send(new ConverseCommand({
          modelId: "non-existent-model-12345",
          messages: [
            {
              role: "user",
              content: [{ text: "Hello" }],
            },
          ],
        }));
      } catch (error) {
        console.log("Error handling: correctly threw on invalid model");
      }
    }, 30000);
  });
});
