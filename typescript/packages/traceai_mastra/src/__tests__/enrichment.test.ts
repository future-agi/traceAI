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
    [SpanType.MODEL_GENERATION, "CHAIN"],
    ["model_inference", "LLM"],
    [SpanType.MODEL_STEP, "CHAIN"],
    [SpanType.TOOL_CALL, "TOOL"],
    [SpanType.MCP_TOOL_CALL, "TOOL"],
    ["client_tool_call", "TOOL"],
    [SpanType.WORKFLOW_RUN, "CHAIN"],
    [SpanType.GENERIC, "CHAIN"],
  ])("maps %s -> %s", (type, kind) => {
    const otelSpan = fakeOtelSpan();
    enrichSpan(otelSpan, fakeMastraSpan(type as SpanType));
    expect(otelSpan.attributes["gen_ai.span.kind"]).toBe(kind);
  });

  it("leaves span kind unset for unmapped types", () => {
    const otelSpan = fakeOtelSpan();
    enrichSpan(otelSpan, fakeMastraSpan("scorer_run" as SpanType));
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


describe("enrichSpan — authoritative converter and runtime fields", () => {
  it("keeps floor generations classified as the actual LLM call", () => {
    const converted = fakeOtelSpan({ "gen_ai.operation.name": "chat" });
    enrichSpan(converted, fakeMastraSpan(SpanType.MODEL_GENERATION));
    expect(converted.attributes["gen_ai.span.kind"]).toBe("LLM");
  });

  it.each([
    ["abort", "caller_cancelled", 2],
    ["abort", "timeout", 2],
    ["error", "runtime_error", 2],
    ["interrupted", "signal_received", 0],
    ["suspend", "approval_required", 0],
    ["limit", "max_steps", 0],
    ["unknown", undefined, 0],
    ["success", undefined, 1],
  ])("preserves %s (%s) without inventing success", (outcome, stopReason, code) => {
    const converted = { attributes: {}, status: { code: 1 } } as unknown as ReadableSpan;
    const span = { ...fakeMastraSpan(SpanType.WORKFLOW_RUN), outcome, stopReason } as unknown as AnyExportedSpan;
    enrichSpan(converted, span);
    expect(converted.status.code).toBe(code);
    expect(converted.attributes["mastra.span.outcome"]).toBe(outcome);
    expect(converted.attributes["mastra.span.stop_reason"]).toBe(stopReason);
  });

  it("does not downgrade an existing error status", () => {
    const converted = { attributes: {}, status: { code: 2 } } as unknown as ReadableSpan;
    const span = { ...fakeMastraSpan(SpanType.WORKFLOW_RUN), outcome: "interrupted" } as unknown as AnyExportedSpan;
    enrichSpan(converted, span);
    expect(converted.status.code).toBe(2);
  });
});
