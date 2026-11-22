/**
 * Regression: the Mastra exporter masks content through its own privacy layer (privacy.ts), not
 * FITracer. The real converter (@mastra/otel-exporter SpanConverter) writes gen_ai.tool.definitions
 * and gen_ai.tool.call.arguments/result, input/output messages. None of the keys newly masked by
 * fi-core 0e0018d3 (tool.parameters, tool.description, gen_ai.prompts, ...) is written by this
 * package; this pins what it does write. Fully offline: the OTLP processor is replaced by a spy.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { FIMastraSpanExporter } from "../FIMastraSpanExporter";

const SECRET = "SYNTH_SECRET_MASTRA_4417";
const ENV = ["FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"];
const saved: Record<string, string | undefined> = {};

beforeEach(() => {
  for (const k of ENV) saved[k] = process.env[k];
});
afterEach(() => {
  for (const k of ENV) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
});

function span(id: string, type: string, extra: Record<string, unknown>, parentSpanId?: string) {
  return {
    id, traceId: "trace-1", name: `${type}-${id}`, type, parentSpanId,
    isRootSpan: parentSpanId === undefined, isEvent: false,
    startTime: new Date(1_000), endTime: new Date(2_000),
    ...extra,
  };
}

async function exportAll(hide: boolean) {
  process.env.FI_HIDE_INPUTS = String(hide);
  process.env.FI_HIDE_OUTPUTS = String(hide);
  const exporter = new FIMastraSpanExporter({ endpoint: "http://127.0.0.1:1/never", headers: {} } as never);
  const exported: any[] = [];
  (exporter as any).processor = { onEnd: (s: unknown) => exported.push(s), forceFlush: async () => {}, shutdown: async () => {} };
  const tools = [{ name: "lookup", description: SECRET, parameters: { q: SECRET } }];
  const spans = [
    span("a", "agent_run", {
      entityId: "agent", entityName: "agent", input: SECRET, output: { text: SECRET },
      attributes: { availableTools: tools, instructions: SECRET },
    }),
    span("t", "tool_call", {
      entityId: "lookup", input: { q: SECRET }, output: { r: SECRET },
      attributes: { toolDescription: SECRET },
    }, "a"),
    span("m", "model_generation", {
      input: [{ role: "user", content: SECRET }], output: { text: SECRET },
      attributes: { model: "m", provider: "p" },
    }, "a"),
  ];
  for (const s of spans) await exporter.exportTracingEvent({ type: "span_ended", exportedSpan: s } as never);
  return exported;
}

describe("Mastra export of content keys under hideInputs/hideOutputs", () => {
  it("can see the secret when nothing is hidden", async () => {
    expect(JSON.stringify(await exportAll(false))).toContain(SECRET);
  });

  it("exports the synthetic secret nowhere, and masks any newly-masked key present", async () => {
    const exported = await exportAll(true);
    expect(exported.length).toBeGreaterThan(0);
    expect(JSON.stringify(exported.map(s => ({ a: s.attributes, e: s.events, n: s.name })))).not.toContain(SECRET);
    const newlyMasked = /^(?:tool\.(?:parameters|description|json_schema)|tool_call\.function\.arguments|gen_ai\.(?:prompts|tool\.parameters|retrieval\.|reranker\.|voice\.|image\.|computer_use\.)|reranker\.)/;
    for (const s of exported) {
      for (const [k, v] of Object.entries(s.attributes)) {
        if (newlyMasked.test(k)) expect(v).toBe("__REDACTED__");
      }
    }
  });
});
