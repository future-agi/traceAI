/**
 * Regression: keys that fi-core / Python mask under hideInputs (tool.description,
 * gen_ai.tool.description, gen_ai.prompts, gen_ai.system_instructions, tool.parameters, ...)
 * must not reach the exported span. VoltAgent spans are exported through this package's own
 * privacy layer (mapping.ts), not FITracer, so this drives a real VoltAgent agent end to end.
 */
import { Agent, createTool } from "@voltagent/core";
import { z } from "zod";
import type { TraceConfigOptions } from "@traceai/fi-core";
import { FIVoltAgentSpanProcessor } from "../FIVoltAgentSpanProcessor";
import { mapVoltAgentAttributes } from "../mapping";
import { SHOW_ALL } from "../privacy";
import {
  MARKERS,
  RecordingProcessor,
  fiProvider,
  resetOtelGlobals,
  scriptedModel,
  settle,
  voltagent,
} from "./helpers";

const SECRET_TOOL_DESC = "SYNTH_SECRET_TOOL_DESCRIPTION_7731";
const SECRET_PARAM = "SYNTH_SECRET_PARAM_DESCRIBE_7732";

const NEWLY_MASKED_KEYS = [
  "tool.description",
  "gen_ai.tool.description",
  "tool.parameters",
  "gen_ai.tool.parameters",
  "gen_ai.prompts",
  "gen_ai.system_instructions",
  "tool.json_schema",
];

let observability: ReturnType<typeof voltagent> | undefined;
afterEach(async () => {
  await resetOtelGlobals(observability);
  observability = undefined;
});

async function run(traceConfig: TraceConfigOptions) {
  const tool = createTool({
    name: "lookup_secret_tool",
    description: SECRET_TOOL_DESC,
    parameters: z.object({ q: z.string().describe(SECRET_PARAM) }),
    execute: async () => ({ ok: true }),
  });
  const { exporter, provider } = fiProvider();
  const recorder = new RecordingProcessor();
  const processor = new FIVoltAgentSpanProcessor({ tracerProvider: provider, traceConfig });
  const { model } = scriptedModel("mockai/mock-model-1", [
    { kind: "tools", calls: [{ id: "c1", name: "lookup_secret_tool", args: { q: "x" } }], input: 3, output: 2 },
    { kind: "text", text: MARKERS.answer, input: 4, output: 2 },
  ]);
  const agent = new Agent({ name: "assistant", instructions: MARKERS.instructions, model, tools: [tool] });
  observability = voltagent({ assistant: agent }, [recorder, processor]);
  await agent.generateText(MARKERS.prompt, { maxSteps: 4 });
  await settle(recorder);
  await processor.forceFlush();
  return exporter.getFinishedSpans();
}

const json = (spans: Awaited<ReturnType<typeof run>>) =>
  JSON.stringify(spans.map((s) => ({ name: s.name, attributes: s.attributes, events: s.events })));

describe("newly masked content keys on VoltAgent export", () => {
  it("shows the tool description when nothing is hidden (the test can see the key)", async () => {
    const spans = await run({ hideInputs: false, hideOutputs: false });
    expect(json(spans)).toContain(SECRET_TOOL_DESC);
  });

  // VoltAgent's own privacy layer (mapping.ts), not fi-core's maskAttributes, must hide these.
  it("exports no tool description / parameters with hideInputs + hideOutputs", async () => {
    const spans = await run({ hideInputs: true, hideOutputs: true });
    const all = json(spans);
    expect(spans.length).toBeGreaterThan(0);
    // Key assertion: the synthetic secrets appear nowhere in the exported span JSON.
    expect(all).not.toContain(SECRET_TOOL_DESC);
    expect(all).not.toContain(SECRET_PARAM);
    for (const span of spans) {
      for (const key of NEWLY_MASKED_KEYS) {
        const value = span.attributes[key];
        if (value !== undefined) expect(value).toBe("__REDACTED__");
      }
    }
  });
});

describe("mapVoltAgentAttributes with hideInputs", () => {
  const policy = { policy: { ...SHOW_ALL, hideInputs: true, hideOutputs: true } };
  it("masks gen_ai.prompts / gen_ai.system_instructions / input", () => {
    const { attributes } = mapVoltAgentAttributes(
      {
        "span.type": "llm",
        "gen_ai.prompts": "SYNTH_PROMPTS_9001",
        "gen_ai.system_instructions": "SYNTH_SYSINSTR_9002",
        "agent.instructions": "SYNTH_INSTR_9003",
      },
      policy,
    );
    const all = JSON.stringify(attributes);
    expect(all).not.toContain("SYNTH_PROMPTS_9001");
    expect(all).not.toContain("SYNTH_SYSINSTR_9002");
    expect(all).not.toContain("SYNTH_INSTR_9003");
  });
});

describe("mapVoltAgentAttributes tool and request content keys", () => {
  const INPUT_KEYS = [
    "tool.description",
    "gen_ai.tool.description",
    "tool.parameters",
    "gen_ai.tool.parameters",
    "tool.json_schema",
    "gen_ai.tool.definitions",
    "gen_ai.tool.call.arguments",
    "gen_ai.request.parameters",
    "gen_ai.agent.description",
  ];
  const OUTPUT_KEYS = ["gen_ai.tool.call.result"];
  const source = (keys: string[]) => Object.fromEntries(keys.map((k) => ["" + k, "SYNTH_" + k]));
  const map = (policy: object, keys: string[]) =>
    JSON.stringify(mapVoltAgentAttributes({ "span.type": "tool", ...source(keys) }, { policy: { ...SHOW_ALL, ...policy } }).attributes);

  it.each(INPUT_KEYS)("hideInputs drops %s and hideOutputs alone keeps it", (key) => {
    expect(map({ hideInputs: true }, [key])).not.toContain("SYNTH_" + key);
    expect(map({ hideOutputs: true }, [key])).toContain("SYNTH_" + key);
    expect(map({}, [key])).toContain("SYNTH_" + key);
  });

  it.each(OUTPUT_KEYS)("hideOutputs drops %s and hideInputs alone keeps it", (key) => {
    expect(map({ hideOutputs: true }, [key])).not.toContain("SYNTH_" + key);
    expect(map({ hideInputs: true }, [key])).toContain("SYNTH_" + key);
    expect(map({}, [key])).toContain("SYNTH_" + key);
  });

  it("keeps names, ids and counts visible under hide flags", () => {
    const out = mapVoltAgentAttributes(
      { "span.type": "tool", "tool.name": "lookup", "tool.call.id": "c1", "gen_ai.tool.name": "lookup" },
      { policy: { ...SHOW_ALL, hideInputs: true, hideOutputs: true } },
    ).attributes;
    expect(out["tool.name"]).toBe("lookup");
    expect(out["tool.call.id"]).toBe("c1");
  });
});
