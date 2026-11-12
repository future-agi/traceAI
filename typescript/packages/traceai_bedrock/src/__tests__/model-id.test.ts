import { afterAll, beforeEach, describe, expect, it, jest } from "@jest/globals";
import { ConverseCommand, InvokeModelCommand } from "@aws-sdk/client-bedrock-runtime";
import { SpanStatusCode } from "@opentelemetry/api";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { BedrockInstrumentation } from "../instrumentation";

const modelIds = [
  "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
  "eu.anthropic.claude-3-5-sonnet-20241022-v2:0",
  "apac.anthropic.claude-3-5-sonnet-20241022-v2:0",
  "us.anthropic.claude-3-5-haiku-20241022-v1:0",
  "anthropic.claude-3-5-sonnet-20241022-v2:0",
  "arn:aws:bedrock:us-east-1:000000000000:inference-profile/us.anthropic.claude-3-5-sonnet-20241022-v2:0",
  "arn:aws:bedrock:us-east-1:000000000000:application-inference-profile/000000000000",
];

const operations: [
  string,
  (modelId: string) => ConverseCommand | InvokeModelCommand,
  unknown,
][] = [
  [
    "bedrock.converse",
    (modelId) =>
      new ConverseCommand({
        modelId,
        messages: [{ role: "user", content: [{ text: "hello" }] }],
      }),
    {
      output: { message: { role: "assistant", content: [{ text: "hello" }] } },
      stopReason: "end_turn",
      usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 },
      metrics: { latencyMs: 1 },
    },
  ],
  [
    "bedrock.invoke_model",
    (modelId) =>
      new InvokeModelCommand({
        modelId,
        contentType: "application/json",
        body: JSON.stringify({
          anthropic_version: "bedrock-2023-05-31",
          max_tokens: 10,
          messages: [{ role: "user", content: [{ type: "text", text: "hello" }] }],
        }),
      }),
    {
      contentType: "application/json",
      body: new TextEncoder().encode(
        JSON.stringify({
          content: [{ type: "text", text: "hello" }],
          usage: { input_tokens: 1, output_tokens: 1 },
        }),
      ),
    },
  ],
];

describe("T8-TS-CLOUD-004 Bedrock request model identity", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });

  beforeEach(() => exporter.reset());
  afterAll(() => provider.shutdown());

  describe.each(operations)("%s", (spanName, makeCommand, response) => {
    it.each(modelIds)("retains the complete requested modelId: %s", async (modelId) => {
      const originalSend = jest.fn(async (_command: unknown) => response);
      // Patch only a fresh fake client; no AWS client, credentials, or network is used.
      class BedrockRuntimeClient {
        send(command: unknown): Promise<unknown> {
          return originalSend(command);
        }
      }
      const instrumentation = new BedrockInstrumentation({
        tracerProvider: provider,
        instrumentationConfig: { enabled: false },
      });
      instrumentation.manuallyInstrument({
        BedrockRuntimeClient,
      } as unknown as Parameters<BedrockInstrumentation["manuallyInstrument"]>[0]);

      const command = makeCommand(modelId);
      const inputBefore = JSON.stringify(command.input);
      const result = await new BedrockRuntimeClient().send(command);

      expect(originalSend).toHaveBeenCalledTimes(1);
      expect(originalSend).toHaveBeenCalledWith(command);
      expect(result).toBe(response);
      expect(JSON.stringify(command.input)).toBe(inputBefore);

      const spans = exporter.getFinishedSpans();
      expect(spans).toHaveLength(1);
      expect(spans[0].name).toBe(spanName);
      expect(spans[0].status.code).toBe(SpanStatusCode.OK);
      expect(spans[0].attributes["gen_ai.request.model"]).toBe(modelId);
    });
  });
});
