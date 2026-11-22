/**
 * An input processor's span output is the (possibly rewritten) request: `messages` and `systemMessages`
 * (@mastra/core buildProcessorSpanOutput, phases "input" and "inputStep"). Under the input hide settings
 * that output must not reach the wire, whichever alias carries it. Fully offline: the OTLP processor is
 * replaced by a spy.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { FIMastraSpanExporter } from "../FIMastraSpanExporter";

const PROMPT = "SYNTH_REQUEST_MESSAGE_5521";
const SYSTEM = "SYNTH_SYSTEM_MESSAGE_5522";
const ANSWER = "SYNTH_MODEL_ANSWER_5523";
const ENV = ["FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_OUTPUT_TEXT"];
const saved: Record<string, string | undefined> = {};

beforeEach(() => { for (const k of ENV) { saved[k] = process.env[k]; delete process.env[k]; } });
afterEach(() => { for (const k of ENV) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; } });

function span(id: string, type: string, extra: Record<string, unknown>, parentSpanId?: string) {
  return {
    id, traceId: "trace-1", name: `${type}-${id}`, type, parentSpanId,
    isRootSpan: parentSpanId === undefined, isEvent: false,
    startTime: new Date(1_000), endTime: new Date(2_000), ...extra,
  };
}

async function exportAll(flags: Record<string, string>) {
  Object.assign(process.env, flags);
  const exporter = new FIMastraSpanExporter({ endpoint: "http://127.0.0.1:1/never", headers: {} } as never);
  const exported: any[] = [];
  (exporter as any).processor = { onEnd: (s: unknown) => exported.push(s), forceFlush: async () => {}, shutdown: async () => {} };
  const spans = [
    span("a", "agent_run", { entityId: "agent", entityName: "agent", input: PROMPT, output: { text: ANSWER } }),
    span("p", "processor_run", {
      entityId: "rewrite", entityName: "rewrite", input: { messages: [{ role: "user", content: "original" }] },
      output: {
        messages: [{ role: "user", content: [{ type: "text", text: PROMPT }] }],
        systemMessages: [{ role: "system", content: SYSTEM }],
      },
    }, "a"),
  ];
  for (const s of spans) await exporter.exportTracingEvent({ type: "span_ended", exportedSpan: s } as never);
  return exported;
}
const dump = (exported: any[]) => JSON.stringify(exported.map(s => ({ a: s.attributes, e: s.events, n: s.name })));
const processorSpan = (exported: any[]) => exported.find(s => String(s.name).startsWith("processor_run"));

describe("processor span output under the input hide settings", () => {
  it("outputOmitsRequestContentWithNoSetting: the control records the rewritten request", async () => {
    const text = dump(await exportAll({}));
    expect(text).toContain(PROMPT);
    expect(text).toContain(SYSTEM);
  });

  it.each(["FI_HIDE_INPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_INPUT_TEXT"])(
    "outputOmitsRequestContentUnderInputSettings (%s)", async (flag) => {
      const exported = await exportAll({ [flag]: "true" });
      const processor = dump([processorSpan(exported)]);
      expect(processor).not.toContain(PROMPT);
      expect(processor).not.toContain(SYSTEM);
    });

  it("output settings still hide the processor output and leave the agent input", async () => {
    const exported = await exportAll({ FI_HIDE_OUTPUTS: "true" });
    expect(dump([processorSpan(exported)])).not.toContain(PROMPT);
    expect(dump(exported)).toContain(PROMPT); // the agent_run input is recorded
  });

  it("the agent's own output text is unaffected by the input settings", async () => {
    const exported = await exportAll({ FI_HIDE_INPUTS: "true" });
    expect(dump(exported)).toContain(ANSWER);
  });
});
