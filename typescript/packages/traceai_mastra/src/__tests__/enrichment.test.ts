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

  it("removes vendor I/O aliases after canonicalization so hide flags have one boundary", () => {
    const otelSpan = fakeOtelSpan({ "mastra.agent_run.input": "private prompt", "mastra.agent_run.output": "private answer" });
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.AGENT_RUN, "private prompt", "private answer"));
    expect(otelSpan.attributes["input.value"]).toBe("private prompt");
    expect(otelSpan.attributes["output.value"]).toBe("private answer");
    expect(otelSpan.attributes["mastra.agent_run.input"]).toBeUndefined();
    expect(otelSpan.attributes["mastra.agent_run.output"]).toBeUndefined();
  });

  it.each([
    [0.12345, 0.6789],
    [[0.12345, 0.6789], [0.9876, 0.54321]],
    { result: [[0.12345, 0.6789]] },
    "[[0.12345,0.6789]]",
  ].map(output => ({ output })))("labels embedding output for the sole vector/output hide boundary (%j)", ({ output }) => {
    const otelSpan = fakeOtelSpan({ "mastra.rag_embedding.output": "unlabelled-vector-alias" });
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.RAG_EMBEDDING, ["text"], output));
    expect(otelSpan.attributes["gen_ai.span.kind"]).toBe("EMBEDDING");
    expect(JSON.parse(otelSpan.attributes["output.value"] as string)).toEqual({ embeddings: output });
    expect(otelSpan.attributes["output.mime_type"]).toBe("application/json");
    expect(otelSpan.attributes["mastra.rag_embedding.output"]).toBeUndefined();
  });

  it("labels existing embedding output without executing application value hooks", () => {
    const hook = jest.fn(() => { throw new Error("application hook"); });
    const date = new Date("2020-01-01T00:00:00Z");
    Object.defineProperty(date, "toJSON", { value: hook });
    const output = Object.defineProperty({ vectors: [0.12345], date }, "getter", { get: hook, enumerable: true });
    const otelSpan = fakeOtelSpan({ "output.value": output });
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.RAG_EMBEDDING));
    expect(JSON.parse(otelSpan.attributes["output.value"] as string)).toEqual({
      embeddings: { vectors: [0.12345], date: "2020-01-01T00:00:00.000Z" },
    });
    expect(hook).not.toHaveBeenCalled();
  });

  it("moves converter-only invocation aliases into the canonical parameter family without losing values", () => {
    const otelSpan = fakeOtelSpan({ "gen_ai.request.top_k": 7, "gen_ai.request.stop_sequences": '["private-stop-value"]', "gen_ai.request.temperature": 0.25 });
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.MODEL_GENERATION));
    expect(otelSpan.attributes["gen_ai.request.top_k"]).toBeUndefined();
    expect(otelSpan.attributes["gen_ai.request.stop_sequences"]).toBeUndefined();
    expect(JSON.parse(otelSpan.attributes["gen_ai.request.parameters"] as string)).toEqual({ top_k: 7, stop_sequences: '["private-stop-value"]' });
    expect(otelSpan.attributes["gen_ai.request.temperature"]).toBe(0.25);
  });

  it("does not overwrite an existing input.value", () => {
    const otelSpan = fakeOtelSpan({ "input.value": "kept" });
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.AGENT_RUN, "new"));
    expect(otelSpan.attributes["input.value"]).toBe("kept");
  });

  it("direct enrichment snapshots I/O and invocation parameters without executing application code", () => {
    const date = new Date("2020-01-01T00:00:00Z");
    const hook = jest.fn(() => { date.setUTCFullYear(2099); throw new Error("user hook"); });
    Object.defineProperty(date, "toJSON", { value: hook });
    const proxy = new Proxy({}, { get: hook, ownKeys: hook, getOwnPropertyDescriptor: hook, getPrototypeOf: hook });
    const input = Object.defineProperty({ date, proxy, fn: hook }, "getter", { enumerable: true, get: hook });
    const originalAttrs = { "gen_ai.request.top_k": date, "gen_ai.request.parameters": proxy };
    const otelSpan = fakeOtelSpan(originalAttrs);
    enrichSpan(otelSpan, fakeMastraSpan(SpanType.WORKFLOW_STEP, input, { date }));
    expect(JSON.parse(otelSpan.attributes["input.value"] as string)).toEqual({ date: "2020-01-01T00:00:00.000Z", proxy: "object", fn: "function" });
    expect(JSON.parse(otelSpan.attributes["output.value"] as string)).toEqual({ date: "2020-01-01T00:00:00.000Z" });
    expect(JSON.parse(otelSpan.attributes["gen_ai.request.parameters"] as string)).toEqual({ top_k: "2020-01-01T00:00:00.000Z", parameters: "object" });
    expect(originalAttrs["gen_ai.request.top_k"]).toBe(date);
    expect(date.getUTCFullYear()).toBe(2020);
    expect(hook).not.toHaveBeenCalled();
  });

  it("does not invoke getters or Proxy traps on enrichment containers", () => {
    const hook = jest.fn(() => { throw new Error("user hook"); });
    const proxy = new Proxy({}, { get: hook, ownKeys: hook, getOwnPropertyDescriptor: hook, getPrototypeOf: hook });
    enrichSpan(proxy as ReadableSpan, fakeMastraSpan(SpanType.AGENT_RUN));
    enrichSpan(Object.defineProperty({}, "attributes", { get: hook }) as ReadableSpan, fakeMastraSpan(SpanType.AGENT_RUN));
    const otelSpan = fakeOtelSpan();
    const span = Object.defineProperty(fakeMastraSpan(SpanType.AGENT_RUN), "input", { get: hook });
    enrichSpan(otelSpan, span);
    expect(otelSpan.attributes["input.value"]).toBeUndefined();
    expect(hook).not.toHaveBeenCalled();
  });
});
