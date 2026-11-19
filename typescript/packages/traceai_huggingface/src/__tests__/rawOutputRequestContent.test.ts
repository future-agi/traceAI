import { InMemorySpanExporter, SimpleSpanProcessor, ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

/**
 * Offline test: the real instrumentation against a stubbed SDK whose textGeneration response carries the
 * request prompt in the SDK's own `details.prefill[].text` field (TextGenerationOutput.details.prefill).
 */
const PROMPT_SENTINEL = "SYNTH-REQUEST-PROMPT-d41a";
const OUTPUT_SENTINEL = "SYNTH-RESPONSE-TEXT-9a02";

const reply = {
  generated_text: OUTPUT_SENTINEL,
  details: {
    finish_reason: "length",
    generated_tokens: 2,
    prefill: [{ id: 1, text: PROMPT_SENTINEL, logprob: -0.1 }],
    tokens: [{ id: 2, text: OUTPUT_SENTINEL, logprob: -0.2, special: false }],
  },
};

async function run(traceConfig: object): Promise<ReadableSpan[]> {
  const exporter = new InMemorySpanExporter();
  const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  let Inst: any;
  jest.isolateModules(() => { Inst = require("../instrumentation").HuggingFaceInstrumentation; });
  const inst = new Inst({ traceConfig });
  inst.setTracerProvider(provider);
  class HfInference {}
  (HfInference.prototype as any).textGeneration = jest.fn(async () => reply);
  inst.manuallyInstrument({ HfInference });
  try {
    await (new HfInference() as any).textGeneration({
      model: "gpt2", inputs: PROMPT_SENTINEL, parameters: { max_new_tokens: 5, details: true },
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

describe("HuggingFace textGeneration raw output and request prompt", () => {
  it("records the response when nothing is hidden (control)", async () => {
    const spans = await run({});
    expect(spans.length).toBeGreaterThan(0);
    expect(dump(spans)).toContain(OUTPUT_SENTINEL);
    expect(spans.some((s) => typeof s.attributes["raw.output"] === "string")).toBe(true);
  });

  it("rawOutputOmitsRequestPromptUnderInputSettings (hideInputs)", async () => {
    const spans = await run({ hideInputs: true });
    expect(spans.length).toBeGreaterThan(0);
    expect(dump(spans)).not.toContain(PROMPT_SENTINEL);
  });
});
