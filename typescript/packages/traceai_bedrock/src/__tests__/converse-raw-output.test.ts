import { describe, it, expect } from "@jest/globals";
import { BedrockRuntimeClient, ConverseCommand } from "@aws-sdk/client-bedrock-runtime";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { BedrockInstrumentation } from "../instrumentation";

const REQUEST_SENTINEL = "REQUEST-SENTINEL-PII-5d1e";
const OUTPUT_SENTINEL = "ASSISTANT-REPLY-2b9c";

// ConverseResponse.trace.guardrail.inputAssessment[*].sensitiveInformationPolicy.piiEntities[].match
// carries the matched text taken from the request.
const response = () => ({
  $metadata: { httpStatusCode: 200 },
  output: { message: { role: "assistant", content: [{ text: OUTPUT_SENTINEL }] } },
  stopReason: "end_turn",
  usage: { inputTokens: 3, outputTokens: 4, totalTokens: 7 },
  metrics: { latencyMs: 5 },
  trace: {
    guardrail: {
      inputAssessment: {
        gr1: {
          sensitiveInformationPolicy: {
            piiEntities: [{ match: REQUEST_SENTINEL, type: "NAME", action: "ANONYMIZED", detected: true }],
          },
        },
      },
    },
  },
});

async function run(traceConfig: Record<string, boolean>) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new BedrockInstrumentation({
    instrumentationConfig: { enabled: false },
    tracerProvider: provider,
    traceConfig,
  });
  instrumentation.manuallyInstrument({ BedrockRuntimeClient } as unknown as Parameters<
    BedrockInstrumentation["manuallyInstrument"]
  >[0]);
  const client = new BedrockRuntimeClient({
    region: "us-east-1",
    credentials: { accessKeyId: "test", secretAccessKey: "test" },
  });
  client.middlewareStack.add(() => async () => ({ output: response(), response: {} }) as never, {
    step: "initialize", name: "stub", priority: "low",
  });
  await client.send(new ConverseCommand({
    modelId: "anthropic.claude-3-haiku",
    messages: [{ role: "user", content: [{ text: `my name is ${REQUEST_SENTINEL}` }] }],
    guardrailConfig: { guardrailIdentifier: "g", guardrailVersion: "1", trace: "enabled" },
  }));
  return JSON.stringify(exporter.getFinishedSpans().map((s) => [s.attributes, s.events]));
}

describe("bedrock converse whole-response attributes", () => {
  it("recordsOutputWhenNoInputFlagIsSet", async () => {
    const dump = await run({});
    expect(dump).toContain(OUTPUT_SENTINEL);
  });

  it.each([["hideInputs"], ["hideInputMessages"], ["hideInputText"]])(
    "rawOutputOmitsRequestContentUnderInputSettings (%s)",
    async (flag) => {
      const dump = await run({ [flag]: true });
      expect(dump).toContain(OUTPUT_SENTINEL);
      expect(dump).not.toContain(REQUEST_SENTINEL);
    },
  );
});
