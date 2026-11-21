import { InMemorySpanExporter, SimpleSpanProcessor, ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

/**
 * Offline test: the real instrumentation against a stubbed SDK whose Completion response carries the
 * request prompt in the SDK's own `prompt[].text` field (together-ai `Completion.prompt`).
 */
const PROMPT_SENTINEL = "SYNTH-REQUEST-PROMPT-b7e1";
const OUTPUT_SENTINEL = "SYNTH-RESPONSE-TEXT-4c9d";

const completionReply = {
  id: "t1", object: "text_completion", created: 1, model: "meta-llama/Llama-3-8b-hf",
  choices: [{ index: 0, text: OUTPUT_SENTINEL, finish_reason: "stop" }],
  prompt: [{ text: PROMPT_SENTINEL, logprobs: { tokens: [PROMPT_SENTINEL], token_logprobs: [-0.1], token_ids: [1] } }],
  usage: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 3 },
};

async function run(traceConfig: object): Promise<ReadableSpan[]> {
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  let Inst: any;
  jest.isolateModules(() => { Inst = require("../instrumentation").TogetherInstrumentation; });
  const inst = new Inst({ traceConfig });
  inst.setTracerProvider(provider);
  class Together {}
  (Together.prototype as any).chat = { completions: { create: jest.fn(async () => ({})) } };
  (Together.prototype as any).completions = { create: jest.fn(async () => completionReply) };
  (Together.prototype as any).embeddings = { create: jest.fn(async () => ({ data: [], usage: {} })) };
  inst.manuallyInstrument({ Together });
  try {
    await (Together.prototype as any).completions.create({
      model: "meta-llama/Llama-3-8b-hf", prompt: PROMPT_SENTINEL, max_tokens: 5, echo: true,
    });
    await provider.forceFlush();
    return exporter.getFinishedSpans();
  } finally {
    inst.disable();
    await provider.shutdown();
  }
}

const dump = (spans: ReadableSpan[]) =>
  JSON.stringify(spans.map((s) => ({ attributes: s.attributes, events: s.events, links: s.links })));

describe("Together completion raw output and request prompt", () => {
  it("records the response when nothing is hidden (control)", async () => {
    const spans = await run({});
    expect(spans.length).toBeGreaterThan(0);
    const all = dump(spans);
    expect(all).toContain(OUTPUT_SENTINEL);
    expect(spans.some((s) => typeof s.attributes["raw.output"] === "string")).toBe(true);
  });

  it.each([
    ["hideInputs", { hideInputs: true }],
  ])("rawOutputOmitsRequestPromptUnderInputSettings (%s)", async (_name, cfg) => {
    const spans = await run(cfg);
    expect(spans.length).toBeGreaterThan(0);
    expect(dump(spans)).not.toContain(PROMPT_SENTINEL);
  });
});
