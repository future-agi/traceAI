import { InMemorySpanExporter, SimpleSpanProcessor, ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

/**
 * Offline regression test for the fi-core content-key masking (maskingRules.ts). Runs the REAL
 * instrumentation of this package against a stubbed SDK, exports through an in-memory exporter
 * and asserts that every synthetic secret on a hidden side, and every content key that Python's
 * TraceConfig masks, is absent or "__REDACTED__" on the exported spans. No network, no credentials.
 */
const REDACTED = "__REDACTED__";
// The content keys fi-core masks (INPUT / OUTPUT / MIXED lists in maskingRules.ts), by prefix.
const INPUT_PREFIXES = [
  "gen_ai.system_instructions", "gen_ai.request.parameters", "gen_ai.request.stop_sequences",
  "gen_ai.agent.description", "gen_ai.image.prompt", "gen_ai.image.negative_prompt", "gen_ai.input.images",
  "gen_ai.prompts", "gen_ai.tool.definitions", "gen_ai.tool.description", "gen_ai.tool.call.arguments",
  "gen_ai.tool.parameters", "tool_call.function.arguments", "message.function_call_arguments_json",
  "gen_ai.computer_use.text", "gen_ai.computer_use.element_selector", "tool.json_schema",
  "gen_ai.retrieval.query", "gen_ai.reranker.query", "gen_ai.reranker.input_documents",
  "reranker.query", "reranker.input_documents", "tool.parameters", "tool.description",
];
const OUTPUT_PREFIXES = [
  "gen_ai.tool.call.result", "gen_ai.image.revised_prompt", "gen_ai.image.output_urls",
  "gen_ai.guardrail.modified_output", "gen_ai.evaluation.explanation", "gen_ai.computer_use.result",
  "retrieval.documents", "gen_ai.retrieval.documents", "gen_ai.reranker.output_documents",
  "reranker.output_documents",
];
const MIXED_PREFIXES = [
  "gen_ai.voice.transcript", "gen_ai.voice.recording.url", "gen_ai.voice.recording.stereo_url",
  "gen_ai.voice.recording.customer_url", "gen_ai.voice.recording.assistant_url", "gen_ai.audio.url",
  "gen_ai.computer_use.screenshot", "gen_ai.computer_use.current_url",
];
const under = (key: string, prefixes: string[]) => prefixes.some((p) => key === p || key.startsWith(`${p}.`));

export interface Secret { value: string; side: "in" | "out" }
type Cfg = { hideInputs?: boolean; hideOutputs?: boolean };

async function collect(
  exercise: (traceConfig: Cfg, provider: NodeTracerProvider) => Promise<void>,
  traceConfig: Cfg,
): Promise<ReadableSpan[]> {
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    await exercise(traceConfig, provider);
    await provider.forceFlush();
    return exporter.getFinishedSpans();
  } finally {
    await provider.shutdown();
  }
}

const dump = (spans: ReadableSpan[]) => JSON.stringify(spans.map((s) => ({
  name: s.name, attributes: s.attributes, events: s.events, links: s.links, status: s.status,
})));

export function defineMaskingContract(
  suite: string,
  exercise: (traceConfig: Cfg, provider: NodeTracerProvider) => Promise<void>,
  secrets: Secret[],
  /** Attribute keys the package must export when nothing is hidden (proves the test is not vacuous). */
  expectedVisibleKeys: string[],
  /** Newly masked key prefixes this package is known to write when nothing is hidden. */
  newlyMaskedKeysWritten: string[] = [],
) {
  describe(suite, () => {
    it("exports the content when nothing is hidden (control)", async () => {
      const spans = await collect(exercise, {});
      expect(spans.length).toBeGreaterThan(0);
      const keys = new Set(spans.flatMap((s) => Object.keys(s.attributes)));
      for (const key of expectedVisibleKeys) expect(keys).toContain(key);
      for (const prefix of newlyMaskedKeysWritten) {
        expect([...keys].some((k) => under(k, [prefix]))).toBe(true);
      }
      const all = dump(spans);
      // at least one secret of each side reaches the span, so the masked runs below are meaningful
      expect(secrets.some((s) => s.side === "in" && all.includes(s.value))).toBe(true);
      expect(secrets.some((s) => s.side === "out" && all.includes(s.value))).toBe(true);
    });

    const matrix: Array<[string, Cfg]> = [
      ["hideInputs", { hideInputs: true }],
      ["hideOutputs", { hideOutputs: true }],
      ["hideInputs + hideOutputs", { hideInputs: true, hideOutputs: true }],
    ];
    it.each(matrix)("%s: no hidden-side secret and no newly masked content key is exported", async (_name, cfg) => {
      const spans = await collect(exercise, cfg);
      expect(spans.length).toBeGreaterThan(0);
      const all = dump(spans);
      for (const span of spans) {
        for (const [key, value] of Object.entries(span.attributes)) {
          const mustMask =
            (cfg.hideInputs && under(key, INPUT_PREFIXES)) ||
            (cfg.hideOutputs && under(key, OUTPUT_PREFIXES)) ||
            ((cfg.hideInputs || cfg.hideOutputs) && under(key, MIXED_PREFIXES));
          if (mustMask) expect([undefined, REDACTED]).toContain(value); // key assertion
        }
      }
      for (const secret of secrets) {
        const hidden = secret.side === "in" ? cfg.hideInputs : cfg.hideOutputs;
        if (hidden) expect(all).not.toContain(secret.value);
      }
    });
  });
}

const IN_TOOL = "SYNTH-IN-TOOL-SCHEMA-7f1";
const IN_ARGS = "SYNTH-IN-TOOLCALL-ARGS-8a2";
const IN_RESULT = "SYNTH-IN-TOOL-RESULT-9b3";
const IN_PROMPT = "SYNTH-IN-PROMPT-1c4";
const OUT_ARGS = "SYNTH-OUT-TOOLCALL-ARGS-2d5";
const OUT_TEXT = "SYNTH-OUT-TEXT-3e6";

const chatArgs = {
  model: "meta-llama/Llama-3.1-8B-Instruct",
  messages: [
    { role: "user", content: `question ${IN_PROMPT}` },
    { role: "assistant", content: null, tool_calls: [
      { id: "call_1", type: "function", function: { name: "lookup", arguments: JSON.stringify({ q: IN_ARGS }) } },
    ] },
    { role: "tool", tool_call_id: "call_1", content: IN_RESULT },
  ],
  tools: [{ type: "function", function: { name: "lookup", description: IN_TOOL,
    parameters: { type: "object", properties: { q: { type: "string", description: IN_TOOL } } } } }],
};
const chatReply = {
  id: "c1", model: "meta-llama/Llama-3.1-8B-Instruct",
  choices: [{ index: 0, finish_reason: "tool_calls", message: { role: "assistant", content: OUT_TEXT,
    tool_calls: [{ id: "call_2", type: "function", function: { name: "lookup", arguments: JSON.stringify({ q: OUT_ARGS }) } }] } }],
  usage: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 3 },
};

async function exercise(traceConfig: object, provider: NodeTracerProvider) {
  let Inst: any;
  // the instrumentation keeps a module-level "patched" flag: take a fresh copy per run
  jest.isolateModules(() => { Inst = require("../instrumentation").HuggingFaceInstrumentation; });
  const inst = new Inst({ traceConfig });
  inst.setTracerProvider(provider);
  class HfInference {}
  const proto = HfInference.prototype as any;
  proto.chatCompletion = jest.fn(async () => chatReply);
  proto.textGeneration = jest.fn(async () => ({ generated_text: OUT_TEXT }));
  inst.manuallyInstrument({ HfInference });
  try {
    const client = new HfInference() as any;
    await client.chatCompletion(chatArgs);
    // textGeneration writes the prompt to gen_ai.prompts.0 (newly masked) besides input.value
    await client.textGeneration({ model: "gpt2", inputs: IN_PROMPT, parameters: { max_new_tokens: 5 } });
  } finally {
    inst.disable();
  }
}

defineMaskingContract(
  "HuggingFace: fi-core content-key masking through the real instrumentation",
  exercise,
  [
    { value: IN_PROMPT, side: "in" }, { value: IN_RESULT, side: "in" },
    { value: OUT_TEXT, side: "out" },
  ],
  ["gen_ai.input.messages", "gen_ai.output.messages"],
  ["gen_ai.prompts"],
);
