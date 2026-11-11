import { SpanType } from "@mastra/core/observability";
import type { AnyExportedSpan } from "@mastra/core/observability";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { enrichSpan, reconcileSpanContext } from "../FIMastraSpanExporter";

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
    ["processor_run", "CHAIN"],
    [SpanType.TOOL_CALL, "TOOL"],
    [SpanType.MCP_TOOL_CALL, "TOOL"],
    ["client_tool_call", "TOOL"],
    [SpanType.WORKFLOW_RUN, "CHAIN"],
    ["workflow_sleep", "CHAIN"],
    ["workflow_wait_event", "CHAIN"],
    ["workflow_conditional_eval", "CHAIN"],
    [SpanType.GENERIC, "CHAIN"],
  ])("maps %s -> %s", (type, kind) => {
    const otelSpan = fakeOtelSpan();
    enrichSpan(otelSpan, fakeMastraSpan(type as SpanType), true);
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


describe("enrichSpan — authoritative runtime fields", () => {
  it("keeps floor generations classified as the actual LLM call", () => {
    const converted = fakeOtelSpan({ "gen_ai.operation.name": "chat" });
    enrichSpan(converted, fakeMastraSpan(SpanType.MODEL_GENERATION), false);
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


describe("enrichSpan — mismatched converter versions", () => {
  it("ignores an old converter's chat and aggregate usage on a new wrapper", () => {
    const converted = fakeOtelSpan({ "gen_ai.operation.name": "chat",
      "gen_ai.usage.input_tokens": 11, "gen_ai.usage.output_tokens": 5,
      "gen_ai.request.model": "synthetic", "gen_ai.response.model": "synthetic",
      "gen_ai.input.messages": "question", "gen_ai.output.messages": "answer",
    });
    enrichSpan(converted, fakeMastraSpan(SpanType.MODEL_GENERATION, "question", "answer"), true);
    expect(converted.attributes["gen_ai.span.kind"]).toBe("CHAIN");
    expect(converted.attributes["gen_ai.operation.name"]).toBe("model_generation");
    expect(Object.keys(converted.attributes).some(key => key.startsWith("gen_ai.usage."))).toBe(false);
    expect(converted.attributes["gen_ai.request.model"]).toBeUndefined();
    expect(converted.attributes["input.value"]).toBe("question");
    expect(converted.attributes["output.value"]).toBe("answer");
  });

  it("does not demote a floor runtime call because the new converter lacks chat", () => {
    const converted = fakeOtelSpan({ "gen_ai.operation.name": "model_generation" });
    enrichSpan(converted, fakeMastraSpan(SpanType.MODEL_GENERATION), false);
    expect(converted.attributes["gen_ai.span.kind"]).toBe("LLM");
  });
});

describe("context reconciliation across converter versions", () => {
  const traceId = "11111111111111111111111111111111";
  const parentId = "2222222222222222";
  const nativeLink = { traceId: "33333333333333333333333333333333", spanId: "4444444444444444" };
  function converted(): ReadableSpan {
    return { ...fakeOtelSpan(), links: [], spanContext: () => ({ traceId, spanId: "5555555555555555", traceFlags: 1 }) } as ReadableSpan;
  }
  it("recovers an external parent and native links without mutating either input", () => {
    const native = { ...fakeMastraSpan(SpanType.AGENT_RUN), traceId, externalParentSpanId: parentId,
      links: [nativeLink, nativeLink] } as unknown as AnyExportedSpan;
    const otel = converted();
    const original = JSON.stringify({ native, otel });
    const result = reconcileSpanContext(otel, native);
    expect(result.parentSpanContext).toEqual({ traceId, spanId: parentId, traceFlags: 1, isRemote: true });
    expect(result.links).toEqual([{ context: { ...nativeLink, traceFlags: 1, isRemote: true } }]);
    expect(JSON.stringify({ native, otel })).toBe(original);
  });
  it("uses a local parent before an inherited external parent", () => {
    const native = { ...fakeMastraSpan(SpanType.TOOL_CALL), traceId, parentSpanId: parentId,
      externalParentSpanId: "6666666666666666" } as unknown as AnyExportedSpan;
    expect(reconcileSpanContext(converted(), native).parentSpanContext).toEqual({ traceId, spanId: parentId, traceFlags: 1, isRemote: false });
  });
  it("retains current converter context and link metadata without appending duplicates", () => {
    const otel = { ...converted(), parentSpanContext: { traceId, spanId: parentId, traceFlags: 0, isRemote: true },
      links: [{ context: { ...nativeLink, traceFlags: 0 }, attributes: { reason: "handoff" } }],
    } as ReadableSpan;
    const native = { ...fakeMastraSpan(SpanType.AGENT_RUN), links: [nativeLink] } as unknown as AnyExportedSpan;
    const result = reconcileSpanContext(otel, native);
    expect(result.parentSpanContext).toBe(otel.parentSpanContext);
    expect(result.links).toEqual(otel.links);
    expect(result.links).not.toBe(otel.links);
  });
  it("does not invent parentage or links for a root", () => {
    const result = reconcileSpanContext(converted(), fakeMastraSpan(SpanType.AGENT_RUN));
    expect(result.parentSpanContext).toBeUndefined();
    expect(result.links).toEqual([]);
  });
});


describe("native provider cost ownership", () => {
  function withCost(type: string, amount: unknown, source: unknown = "provider_reported", unit: unknown = "USD"): AnyExportedSpan {
    return { ...fakeMastraSpan(type as SpanType), attributes: {
      costContext: { estimatedCost: amount, costUnit: unit, costMetadata: { source } },
    } } as unknown as AnyExportedSpan;
  }

  it.each([false, true])("preserves precise provider costs and zero with inference runtime=%s", inferenceEnabled => {
    for (const amount of [0, 0.1234567]) {
      const native = withCost("model_generation", amount);
      const original = JSON.stringify(native);
      const converted = fakeOtelSpan({ "gen_ai.cost.total": 999 });
      enrichSpan(converted, native, inferenceEnabled);
      expect(converted.attributes).toMatchObject({ "mastra.cost.amount": amount,
        "mastra.cost.unit": "USD", "mastra.cost.source": "provider_reported", "gen_ai.cost.total": amount });
      expect(JSON.stringify(native)).toBe(original);
    }
  });

  it.each([NaN, Infinity, -Infinity, -1, "0.1", null, undefined])("does not export invalid amount %s", amount => {
    const converted = fakeOtelSpan();
    enrichSpan(converted, withCost("model_generation", amount), true);
    expect(Object.keys(converted.attributes).filter(key => key.includes("cost."))).toEqual([]);
  });

  it.each([["token_price_estimate", "USD"], [undefined, "USD"], ["provider_reported", "EUR"]])(
    "retains %s / %s only as diagnostic cost, not a provider USD charge", (source, unit) => {
      // Null avoids the helper's default when the provider source is absent.
      const native = withCost("model_generation", 0.25, source ?? null, unit);
      const converted = fakeOtelSpan();
      enrichSpan(converted, native, true);
      expect(converted.attributes["mastra.cost.amount"]).toBe(0.25);
      expect(converted.attributes["mastra.cost.unit"]).toBe(unit);
      expect(converted.attributes["mastra.cost.source"]).toBe(source);
      expect(converted.attributes["gen_ai.cost.total"]).toBeUndefined();
    });

  it("does not replace a converter's generation charge with a non-provider estimate", () => {
    const converted = fakeOtelSpan({ "gen_ai.cost.total": 0.5 });
    enrichSpan(converted, withCost("model_generation", 0.25, "token_price_estimate"), true);
    expect(converted.attributes["gen_ai.cost.total"]).toBe(0.5);
    expect(converted.attributes["mastra.cost.amount"]).toBe(0.25);
  });

  it("lets only the generation own the total when inference repeats its costContext", () => {
    const generation = fakeOtelSpan();
    const inference = fakeOtelSpan({ "gen_ai.cost.total": 0.1234567, "llm.cost.total": 0.1234567 });
    enrichSpan(generation, withCost("model_generation", 0.1234567), true);
    enrichSpan(inference, withCost("model_inference", 0.1234567), true);
    expect(generation.attributes["gen_ai.cost.total"]).toBe(0.1234567);
    expect(inference.attributes["mastra.cost.amount"]).toBe(0.1234567);
    expect(inference.attributes["gen_ai.cost.total"]).toBeUndefined();
    expect(inference.attributes["llm.cost.total"]).toBeUndefined();
  });
});

describe("enrichSpan — attachment references", () => {
  it("never forwards fi.attachment.* keys it did not issue", () => {
    const otelSpan = fakeOtelSpan({
      "fi.attachment.schema_version": 1,
      "fi.attachment.refs": JSON.stringify([{ attachment_id: "forged", occurrence_id: "forged" }]),
      "fi.attachment.capture_session_id": "forged",
      "fi.attachment.correlation_nonce": "forged",
      "mastra.metadata.note": "kept",
    });
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.AGENT_RUN), true);
    expect(Object.keys(otelSpan.attributes).filter(key => key.startsWith("fi.attachment."))).toEqual([]);
    expect(otelSpan.attributes["mastra.metadata.note"]).toBe("kept");
  });
});
