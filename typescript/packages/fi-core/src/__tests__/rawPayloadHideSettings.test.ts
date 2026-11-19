import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { SemanticConventions as SC } from "@traceai/fi-semantic-conventions";
import { FITracer } from "../trace/trace-config/FITracer";
import { TraceConfigOptions } from "../trace/trace-config/types";
import { maskAttributes } from "../trace/trace-config/maskingRules";
import { generateTraceConfig } from "../trace/trace-config/traceConfig";

const REQUEST = "SYNTH-RAW-REQUEST-BODY-5d21";
const RESPONSE = "SYNTH-RAW-RESPONSE-BODY-9a7c";

const settings: (keyof TraceConfigOptions)[] = [
  "hideInputs",
  "hideOutputs",
  "hideInputMessages",
  "hideOutputMessages",
  "hideInputText",
  "hideOutputText",
  "hideInputImages",
];

describe("raw input and raw output are not recorded while any hide setting is on", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  afterEach(() => exporter.reset());
  afterAll(() => provider.shutdown());

  const payload = {
    [SC.LLM_MODEL_NAME]: "synthetic-model",
    [SC.RAW_INPUT]: JSON.stringify({ prompt: REQUEST }),
    [SC.RAW_OUTPUT]: JSON.stringify({ echo: REQUEST, text: RESPONSE }),
  };

  it("keeps both raw attributes when no setting is on (control)", () => {
    const masked = maskAttributes({ config: generateTraceConfig({}), attributes: payload });
    expect(masked[SC.RAW_INPUT]).toBe(payload[SC.RAW_INPUT]);
    expect(masked[SC.RAW_OUTPUT]).toBe(payload[SC.RAW_OUTPUT]);
  });

  it.each(settings)("%s drops raw.input and raw.output and keeps safe metadata", (setting) => {
    const masked = maskAttributes({ config: generateTraceConfig({ [setting]: true }), attributes: payload });
    expect(masked[SC.RAW_INPUT]).toBeUndefined();
    expect(masked[SC.RAW_OUTPUT]).toBeUndefined();
    expect(masked[SC.LLM_MODEL_NAME]).toBe("synthetic-model");
  });

  for (const setting of settings) {
    for (const placement of ["creation", "setter"] as const) {
      it(`${setting} drops raw attributes set at ${placement}`, () => {
        const tracer = new FITracer({ tracer: provider.getTracer("raw"), traceConfig: { [setting]: true } });
        const span = tracer.startSpan("test", { attributes: placement === "creation" ? payload : {} });
        if (placement === "setter") span.setAttributes(payload);
        span.end();
        const [exported] = exporter.getFinishedSpans();
        expect(exported.attributes[SC.RAW_INPUT]).toBeUndefined();
        expect(exported.attributes[SC.RAW_OUTPUT]).toBeUndefined();
        expect(JSON.stringify(exported.attributes)).not.toContain(REQUEST);
        expect(JSON.stringify(exported.attributes)).not.toContain(RESPONSE);
      });
    }
  }
});
