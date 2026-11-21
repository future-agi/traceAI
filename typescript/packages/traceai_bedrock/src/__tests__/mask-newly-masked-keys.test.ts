import { describe, it, expect } from "@jest/globals";
import { BedrockAgentRuntimeClient, InvokeAgentCommand } from "@aws-sdk/client-bedrock-agent-runtime";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { BedrockInstrumentation } from "../instrumentation";

const SECRET_DESC = "SECRET-TOOL-DESCRIPTION-7f3a";
const SECRET_PARAM = "SECRET-PARAM-VALUE-91bc";

const enc = new TextEncoder();
async function* events() {
  yield {
    trace: {
      agentId: "A", agentAliasId: "B", sessionId: "s",
      trace: {
        orchestrationTrace: {
          invocationInput: {
            traceId: "t-1",
            invocationType: "ACTION_GROUP",
            actionGroupInvocationInput: {
              actionGroupName: "grp",
              function: "do_thing",
              description: SECRET_DESC,
              executionType: "LAMBDA",
              parameters: [{ name: "p", type: "string", value: SECRET_PARAM }],
            },
          },
        },
      },
    },
  };
  yield { chunk: { bytes: enc.encode("done") } };
}

describe("bedrock agent runtime: newly masked content keys under hideInputs", () => {
  it("does not export tool.description / tool.parameters / tool_call arguments", async () => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const instrumentation = new BedrockInstrumentation({
      instrumentationConfig: { enabled: false },
      tracerProvider: provider,
      traceConfig: { hideInputs: true, hideOutputs: true },
    });
    instrumentation.manuallyInstrument({ BedrockAgentRuntimeClient } as unknown as Parameters<
      BedrockInstrumentation["manuallyInstrument"]
    >[0]);
    const client = new BedrockAgentRuntimeClient({
      region: "us-east-1",
      credentials: { accessKeyId: "test", secretAccessKey: "test" },
    });
    client.middlewareStack.add(() => async () => ({ output: { completion: events() }, response: {} }) as never, {
      step: "initialize", name: "stub", priority: "low",
    });
    const res = await client.send(
      new InvokeAgentCommand({ agentId: "A", agentAliasId: "B", sessionId: "s", inputText: "hi", enableTrace: true }),
    );
    for await (const _e of res.completion as AsyncIterable<unknown>) { /* drain */ }

    const spans = exporter.getFinishedSpans();
    const tool = spans.find((s) => s.name === "execute_tool do_thing");
    expect(tool).toBeDefined();
    const dump = JSON.stringify(spans.map((s) => s.attributes));
    // key assertion: the synthetic secrets appear nowhere in exported spans
    expect(dump).not.toContain(SECRET_DESC);
    expect(dump).not.toContain(SECRET_PARAM);
    for (const [k, v] of Object.entries(tool!.attributes)) {
      if (k.startsWith("tool.description") || k.startsWith("tool.parameters") || k.includes("function.arguments")) {
        expect(v).toBe("__REDACTED__");
      }
    }
    expect(JSON.stringify(spans.map((s) => s.attributes))).not.toContain(SECRET_DESC);
  });
});
