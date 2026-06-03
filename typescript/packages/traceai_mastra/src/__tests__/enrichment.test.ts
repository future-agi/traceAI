import { SpanType } from "@mastra/core/observability";
import type { AnyExportedSpan } from "@mastra/core/observability";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { enrichSpan } from "../FIMastraSpanExporter";

function fakeOtelSpan(
  attributes: Record<string, unknown> = {},
): ReadableSpan {
  return { attributes } as unknown as ReadableSpan;
}

function fakeMastraSpan(
  type: SpanType,
  input?: unknown,
  output?: unknown,
): AnyExportedSpan {
  return { id: "span-1", traceId: "trace-1", type, input, output } as unknown as AnyExportedSpan;
}

describe("enrichSpan — gen_ai.span.kind mapping", () => {
  it.each([
    [SpanType.AGENT_RUN, "AGENT"],
    [SpanType.MODEL_GENERATION, "LLM"],
    [SpanType.MODEL_INFERENCE, "LLM"],
    [SpanType.MODEL_STEP, "CHAIN"],
    [SpanType.TOOL_CALL, "TOOL"],
    [SpanType.MCP_TOOL_CALL, "TOOL"],
    [SpanType.CLIENT_TOOL_CALL, "TOOL"],
    [SpanType.WORKFLOW_RUN, "CHAIN"],
    [SpanType.GENERIC, "CHAIN"],
  ])("maps %s -> %s", (type, kind) => {
    const otelSpan = fakeOtelSpan();
    enrichSpan(otelSpan, fakeMastraSpan(type as SpanType));
    expect(otelSpan.attributes["gen_ai.span.kind"]).toBe(kind);
  });

  it("leaves span kind unset for unmapped types", () => {
    const otelSpan = fakeOtelSpan();
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.SCORER_RUN));
    expect(otelSpan.attributes["gen_ai.span.kind"]).toBeUndefined();
  });

  it("does not overwrite an existing gen_ai.span.kind", () => {
    const otelSpan = fakeOtelSpan({ "gen_ai.span.kind": "EXISTING" });
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.AGENT_RUN));
    expect(otelSpan.attributes["gen_ai.span.kind"]).toBe("EXISTING");
  });
});

describe("enrichSpan — input.value / output.value", () => {
  it("serializes object input as JSON with the json mime type", () => {
    const otelSpan = fakeOtelSpan();
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.AGENT_RUN, { q: "hi" }));
    expect(otelSpan.attributes["input.value"]).toBe(JSON.stringify({ q: "hi" }));
    expect(otelSpan.attributes["input.mime_type"]).toBe("application/json");
  });

  it("stores string input as text/plain", () => {
    const otelSpan = fakeOtelSpan();
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.AGENT_RUN, "hello"));
    expect(otelSpan.attributes["input.value"]).toBe("hello");
    expect(otelSpan.attributes["input.mime_type"]).toBe("text/plain");
  });

  it("sets output.value from the span output", () => {
    const otelSpan = fakeOtelSpan();
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.TOOL_CALL, undefined, { result: 1 }));
    expect(otelSpan.attributes["output.value"]).toBe(JSON.stringify({ result: 1 }));
    expect(otelSpan.attributes["output.mime_type"]).toBe("application/json");
  });

  it("skips input/output when null or undefined", () => {
    const otelSpan = fakeOtelSpan();
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.AGENT_RUN, null, undefined));
    expect(otelSpan.attributes["input.value"]).toBeUndefined();
    expect(otelSpan.attributes["output.value"]).toBeUndefined();
  });

  it("does not overwrite an existing input.value", () => {
    const otelSpan = fakeOtelSpan({ "input.value": "kept" });
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.AGENT_RUN, "new"));
    expect(otelSpan.attributes["input.value"]).toBe("kept");
  });
});
