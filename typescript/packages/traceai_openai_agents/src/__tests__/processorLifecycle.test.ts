import * as http from "http";
import { AddressInfo } from "net";
import type * as AgentsSDK from "@openai/agents/dist/index";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracingProcessor, OpenAIAgentsInstrumentation, _resetPatchedStateForTesting } from "../instrumentation";

const agents: typeof AgentsSDK = require("@openai/agents");
// Use the same real OTLP exporter as fi-core without a second exporter dependency.
const corePaths = [require.resolve("@traceai/fi-core")];
const { OTLPTraceExporter } = require(require.resolve("@opentelemetry/exporter-trace-otlp-http", { paths: corePaths }));
const { BasicTracerProvider: WireProvider, SimpleSpanProcessor: WireProcessor } = require(require.resolve("@opentelemetry/sdk-trace-base", { paths: corePaths }));

const START = "2026-01-01T00:00:00.123Z";
const END = "2026-01-01T00:00:00.456Z";
const ROOT = { traceId: "trace_contract", name: "workflow", groupId: "conversation-42" };
type CallbackSpan = Parameters<FITracingProcessor["onSpanStart"]>[0];

function nativeSpan(processor: FITracingProcessor, data: AgentsSDK.SpanData, id = "span_contract") {
  return new agents.Span({ traceId: ROOT.traceId, spanId: id, startedAt: START, endedAt: END, data },
    processor);
}
function emit(processor: FITracingProcessor, span: ReturnType<typeof nativeSpan>) {
  // Also exercises the native SDK's null-valued parent/error accessors.
  processor.onSpanStart(span as unknown as CallbackSpan);
  processor.onSpanEnd(span as unknown as CallbackSpan);
}

function setup(options: { traceMetadataKeys?: string[]; traceConfig?: { hideInputs?: boolean; hideOutputs?: boolean } } = {}) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const instrumentation = new OpenAIAgentsInstrumentation(options);
  instrumentation.setTracerProvider(provider);
  instrumentation.enable();
  return { exporter, provider, instrumentation, processor: instrumentation.getProcessor() };
}

describe("processor semantics and lifecycle", () => {
  let fixture: ReturnType<typeof setup>;
  beforeEach(() => { _resetPatchedStateForTesting(); fixture = setup(); });
  afterEach(async () => { fixture.instrumentation.disable(); await fixture.provider.shutdown(); });

  it("preserves groupId and only explicitly approved trace metadata", async () => {
    fixture.instrumentation.disable();
    await fixture.provider.shutdown();
    fixture = setup({ traceMetadataKeys: ["release", "count"] });
    const root = new agents.Trace({ ...ROOT, metadata: { release: "canary", count: 0, secret: "not-approved" } });
    fixture.processor.onTraceStart(root);
    fixture.processor.onTraceEnd(root);
    const [span] = fixture.exporter.getFinishedSpans();
    expect(span.attributes["session.id"]).toBe(ROOT.groupId);
    expect(JSON.parse(String(span.attributes.metadata))).toEqual({ release: "canary", count: 0 });
    expect(JSON.stringify(span.attributes)).not.toContain("not-approved");
  });

  it("does not export arbitrary metadata or tracing credentials by default", () => {
    const root = new agents.Trace({ ...ROOT, metadata: { secret: "private-metadata" }, tracingApiKey: "fake-private-key" });
    fixture.processor.onTraceStart(root);
    fixture.processor.onTraceEnd(root);
    const [span] = fixture.exporter.getFinishedSpans();
    expect(span.attributes.metadata).toBeUndefined();
    expect(JSON.stringify(span.attributes)).not.toMatch(/private-metadata|fake-private-key/);
  });

  it("uses native start/end timestamps even when callbacks are delayed", () => {
    emit(fixture.processor, nativeSpan(fixture.processor, { type: "guardrail", name: "policy", triggered: false }));
    const [span] = fixture.exporter.getFinishedSpans();
    expect(span.startTime).toEqual([1767225600, 123000000]);
    expect(span.endTime).toEqual([1767225600, 456000000]);
    expect(span.duration).toEqual([0, 333000000]);
  });

  it("maps guardrails and MCP tool discovery without a content-bearing raw dump", () => {
    fixture.processor.onTraceStart(ROOT);
    emit(fixture.processor, nativeSpan(fixture.processor, { type: "guardrail", name: "policy", triggered: false }));
    emit(fixture.processor, nativeSpan(fixture.processor, { type: "mcp_tools", server: "catalog", result: ["lookup"] }, "span_mcp"));
    fixture.processor.onTraceEnd(ROOT);
    const [guardrail, mcp, root] = fixture.exporter.getFinishedSpans();
    expect(guardrail.attributes["fi.span.kind"]).toBe("GUARDRAIL");
    expect(guardrail.attributes["guardrail.triggered"]).toBe(false);
    expect(mcp.attributes["fi.span.kind"]).toBe("TOOL");
    expect(mcp.attributes["mcp.server.name"]).toBe("catalog");
    expect(mcp.attributes["tool.name"]).toBe("list_tools");
    expect(JSON.parse(String(mcp.attributes["output.value"]))).toEqual(["lookup"]);
    expect(guardrail.parentSpanId).toBe(root.spanContext().spanId);
    expect(mcp.parentSpanId).toBe(root.spanContext().spanId);
    expect(mcp.attributes["raw.input"]).toBeUndefined();
  });

  it("ends outstanding spans once, child before root, and ignores all callbacks after shutdown", () => {
    const { processor, exporter } = fixture;
    processor.onTraceStart(ROOT);
    const child = nativeSpan(processor, { type: "function", name: "pending", input: "", output: "" });
    processor.onSpanStart(child as unknown as CallbackSpan);
    processor.shutdown();
    processor.shutdown();
    processor.onSpanEnd(child as unknown as CallbackSpan);
    processor.onTraceEnd(ROOT);
    processor.onTraceStart({ ...ROOT, traceId: "late" });
    emit(processor, nativeSpan(processor, { type: "guardrail", name: "late", triggered: true }, "span_late"));
    processor.forceFlush();
    const spans = exporter.getFinishedSpans();
    expect(spans.map(s => s.name)).toEqual(["pending", "workflow"]);
    expect(spans.every(s => s.status.code === 2)).toBe(true);
  });

  it("does not abandon spans on duplicate start callbacks", () => {
    const { processor, exporter } = fixture;
    processor.onTraceStart(ROOT);
    processor.onTraceStart(ROOT);
    const child = nativeSpan(processor, { type: "guardrail", name: "policy", triggered: true });
    processor.onSpanStart(child as unknown as CallbackSpan);
    processor.onSpanStart(child as unknown as CallbackSpan);
    processor.onSpanEnd(child as unknown as CallbackSpan);
    processor.onTraceEnd(ROOT);
    expect(exporter.getFinishedSpans()).toHaveLength(2);
    const [span, root] = exporter.getFinishedSpans();
    expect(span.parentSpanId).toBe(root.spanContext().spanId);
  });

  it("falls back safely for invalid timestamps", () => {
    const before = Date.now();
    const child = { traceId: ROOT.traceId, spanId: "bad-time", startedAt: "not-a-date", endedAt: "invalid", spanData: { type: "agent" } };
    fixture.processor.onSpanStart(child);
    fixture.processor.onSpanEnd(child);
    const [span] = fixture.exporter.getFinishedSpans();
    expect(span.startTime[0] * 1000).toBeGreaterThanOrEqual(before - 1000);
    expect(span.duration[0]).toBeGreaterThanOrEqual(0);
  });

  it("receives native guardrail and MCP callbacks through the SDK registration", async () => {
    agents.setTracingDisabled(false);
    fixture.instrumentation.manuallyInstrument(agents);
    await agents.withTrace("native workflow", async root => {
      const guardrail = agents.createGuardrailSpan({ data: { name: "policy", triggered: true } }, root);
      guardrail.start();
      guardrail.end();
      const mcp = agents.createMCPListToolsSpan({ data: { server: "catalog", result: ["lookup"] } }, root);
      mcp.start();
      mcp.end();
      await agents.getGlobalTraceProvider().forceFlush();
      const [guardSpan, mcpSpan] = fixture.exporter.getFinishedSpans();
      expect(guardSpan.attributes["fi.span.kind"]).toBe("GUARDRAIL");
      expect(guardSpan.attributes["guardrail.triggered"]).toBe(true);
      expect(mcpSpan.attributes["fi.span.kind"]).toBe("TOOL");
      for (const [native, otel] of [[guardrail, guardSpan], [mcp, mcpSpan]] as const) {
        expect(otel.startTime[0] * 1000 + otel.startTime[1] / 1e6).toBe(Date.parse(native.startedAt!));
        expect(otel.endTime[0] * 1000 + otel.endTime[1] / 1e6).toBe(Date.parse(native.endedAt!));
      }
    }, { groupId: "native-session" });
    const spans = fixture.exporter.getFinishedSpans();
    expect(spans).toHaveLength(3);
    expect(spans[2].attributes["session.id"]).toBe("native-session");
    agents.setTraceProcessors([]);
  });

  it("allows retry after failed registration without poisoning the global state", () => {
    const module = { setTraceProcessors: jest.fn((): void => { throw new Error("synthetic registration failure"); }), addTraceProcessor: jest.fn() };
    fixture.instrumentation.manuallyInstrument(module);
    module.setTraceProcessors.mockImplementation(() => {});
    fixture.instrumentation.manuallyInstrument(module);
    expect(module.setTraceProcessors).toHaveBeenCalledTimes(2);
  });

  it("disables manually registered callbacks and re-enables without duplicate spans", async () => {
    agents.setTracingDisabled(false);
    // A second processor stands in for a separately configured exporter; no vendor calls.
    const other = { onTraceStart: jest.fn(async () => {}), onTraceEnd: jest.fn(async () => {}), onSpanStart: jest.fn(async () => {}), onSpanEnd: jest.fn(async () => {}), forceFlush: jest.fn(async () => {}), shutdown: jest.fn(async () => {}) };
    agents.setTraceProcessors([other]);
    fixture.instrumentation.disable();
    await fixture.provider.shutdown();
    const config = { exclusiveProcessor: false };
    fixture = setup();
    fixture.instrumentation.disable();
    fixture.instrumentation = new OpenAIAgentsInstrumentation(config);
    fixture.instrumentation.setTracerProvider(fixture.provider);
    fixture.instrumentation.manuallyInstrument(agents);
    await agents.withTrace("before", async () => {});
    fixture.instrumentation.disable();
    await agents.withTrace("disabled", async () => {});
    fixture.instrumentation.enable();
    fixture.instrumentation.manuallyInstrument(agents);
    await agents.withTrace("after", async () => {});
    expect(fixture.exporter.getFinishedSpans().map(s => s.name)).toEqual(["before", "after"]);
    expect(other.onTraceEnd).toHaveBeenCalledTimes(3);
    agents.setTraceProcessors([]);
  });
});

describe("OTLP wire contract", () => {
  it.each([false, true])("exports native times and parentage with hideOutputs=%s", async hideOutputs => {
    const bodies: string[] = [];
    const server = http.createServer((req, res) => {
      let body = "";
      req.on("data", chunk => { body += chunk; });
      req.on("end", () => { bodies.push(body); res.writeHead(200, { "content-type": "application/json" }); res.end("{}"); });
    });
    await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
    const { port } = server.address() as AddressInfo;
    const provider = new WireProvider({ spanProcessors: [new WireProcessor(new OTLPTraceExporter({ url: `http://127.0.0.1:${port}/v1/traces` }))] });
    const instrumentation = new OpenAIAgentsInstrumentation({ traceMetadataKeys: ["release"], traceConfig: { hideInputs: !hideOutputs, hideOutputs } });
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
    const processor = instrumentation.getProcessor();
    try {
      processor.onTraceStart({ ...ROOT, metadata: { release: "canary", secret: "wire-private-metadata" } });
      emit(processor, nativeSpan(processor, { type: "mcp_tools", server: "catalog", result: ["wire-private-tool"] }));
      emit(processor, nativeSpan(processor, { type: "custom", name: "custom", data: { value: "wire-private-custom" } }, "span_custom"));
      processor.onTraceEnd(ROOT);
      await provider.forceFlush();
      const wire = bodies.join("\n");
      expect(wire).not.toContain("wire-private-metadata");
      if (hideOutputs) expect(wire).not.toMatch(/wire-private-tool|wire-private-custom/);
      const spans = bodies.flatMap(body => JSON.parse(body).resourceSpans.flatMap((r: any) => r.scopeSpans.flatMap((s: any) => s.spans)));
      const child = spans.find((s: any) => s.name === "mcp_tools");
      const root = spans.find((s: any) => s.name === ROOT.name);
      expect(String(child.startTimeUnixNano)).toBe("1767225600123000000");
      expect(String(child.endTimeUnixNano)).toBe("1767225600456000000");
      expect(child.parentSpanId).toBe(root.spanId);
      const attrs = Object.fromEntries(child.attributes.map((a: any) => [a.key, a.value.stringValue]));
      expect(attrs["fi.span.kind"]).toBe("TOOL");
      expect(attrs["output.value"]).toBe(hideOutputs ? "__REDACTED__" : '["wire-private-tool"]');
      const rootAttrs = Object.fromEntries(root.attributes.map((a: any) => [a.key, a.value.stringValue]));
      expect(rootAttrs["session.id"]).toBe(ROOT.groupId);
      expect(JSON.parse(rootAttrs.metadata)).toEqual({ release: "canary" });
      const custom = spans.find((s: any) => s.name === "custom");
      const customAttrs = Object.fromEntries(custom.attributes.map((a: any) => [a.key, a.value.stringValue]));
      expect(customAttrs["output.value"]).toBe(hideOutputs ? "__REDACTED__" : '{"value":"wire-private-custom"}');
    } finally {
      instrumentation.disable();
      await provider.shutdown();
      server.closeAllConnections();
      await new Promise<void>(resolve => server.close(() => resolve()));
    }
  });
});
