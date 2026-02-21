/**
 * E2E Tests for @traceai/bedrock
 *
 * These tests run against AWS Bedrock and export spans
 * to the FI backend via register() from @traceai/fi-core.
 *
 * Required environment variables:
 *   FI_API_KEY            - FI platform API key
 *   FI_SECRET_KEY         - FI platform secret key (if required)
 *   AWS_ACCESS_KEY_ID     - AWS access key
 *   AWS_SECRET_ACCESS_KEY - AWS secret key
 *   AWS_REGION            - AWS region (defaults to us-east-1)
 *
 * Run with: FI_API_KEY=... AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... pnpm test -- --testPathPattern=e2e
 */
import { register, FITracerProvider } from "@traceai/fi-core";
import { BedrockInstrumentation } from "../instrumentation";

const FI_API_KEY = process.env.FI_API_KEY;
const AWS_ACCESS_KEY_ID = process.env.AWS_ACCESS_KEY_ID;
const AWS_SECRET_ACCESS_KEY = process.env.AWS_SECRET_ACCESS_KEY;
const AWS_REGION = process.env.AWS_REGION || "us-east-1";

const describeE2E = FI_API_KEY && AWS_ACCESS_KEY_ID && AWS_SECRET_ACCESS_KEY
  ? describe
  : describe.skip;

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
        accessKeyId: AWS_ACCESS_KEY_ID!,
        secretAccessKey: AWS_SECRET_ACCESS_KEY!,
      },
    });
  });

  afterAll(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  describe("Converse", () => {
    it("should complete a basic converse request", async () => {
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
      expect(response.output.message).toBeDefined();
      console.log("Converse response:", JSON.stringify(response.output.message));
    }, 30000);

    it("should handle system prompt", async () => {
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
    }, 30000);
  });

  describe("InvokeModel", () => {
    it("should invoke a model directly", async () => {
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
    }, 30000);
  });

  describe("Error Handling", () => {
    it("should handle invalid model gracefully", async () => {
      await expect(
        client.send(new ConverseCommand({
          modelId: "non-existent-model-12345",
          messages: [
            {
              role: "user",
              content: [{ text: "Hello" }],
            },
          ],
        }))
      ).rejects.toThrow();
      console.log("Error handling: correctly threw on invalid model");
    }, 30000);
  });
});
