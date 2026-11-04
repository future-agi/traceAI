import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { SemanticConventions as SC } from "@traceai/fi-semantic-conventions";
import { FITracer } from "../trace/trace-config/FITracer";
import { TraceConfigOptions } from "../trace/trace-config/types";

const retrievalKeys = ["retrieval.documents", "retrieval.documents.0.document.content", "retrieval.documents.0.document.metadata", "retrieval.documents.12.document.content", "retrieval.documents.12.document.metadata"];
const cases: [keyof TraceConfigOptions, string[]][] = [
  ["hideOutputs", retrievalKeys],
  ["hideInputs", [SC.PROMPT_TEMPLATE_VARIABLES, `${SC.EMBEDDING_EMBEDDINGS}.0.${SC.EMBEDDING_TEXT}`, `${SC.EMBEDDING_EMBEDDINGS}.12.${SC.EMBEDDING_TEXT}`]],
  ["hideInputText", [SC.PROMPT_TEMPLATE_VARIABLES, SC.INPUT_VALUE, SC.RAW_INPUT, `${SC.EMBEDDING_EMBEDDINGS}.0.${SC.EMBEDDING_TEXT}`]],
  ["hideInputImages", [SC.PROMPT_TEMPLATE_VARIABLES, SC.INPUT_VALUE, SC.RAW_INPUT]],
  ["hideInputMessages", [SC.PROMPT_TEMPLATE_VARIABLES, SC.INPUT_VALUE, SC.RAW_INPUT]],
  ["hideOutputText", [SC.OUTPUT_VALUE, SC.RAW_OUTPUT, ...retrievalKeys]],
  ["hideOutputMessages", [SC.OUTPUT_VALUE, SC.RAW_OUTPUT]],
  ["hideEmbeddingVectors", [SC.OUTPUT_VALUE, SC.RAW_OUTPUT, `${SC.EMBEDDING_EMBEDDINGS}.0.${SC.EMBEDDING_VECTOR}`]],
];

describe("privacy covers duplicate payload attributes", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  afterEach(() => exporter.reset());
  afterAll(() => provider.shutdown());

  for (const [flag, keys] of cases) {
    for (const placement of ["creation", "setter", "link"] as const) {
      it(`${flag} protects ${placement} attributes without removing safe metadata`, () => {
        const attributes = Object.fromEntries(keys.map(key => [key, "SYNTHETIC-PRIVATE-PAYLOAD"]));
        const tracer = new FITracer({ tracer: provider.getTracer("privacy"), traceConfig: { [flag]: true } });
        const span = tracer.startSpan("test", {
          attributes: { [SC.LLM_MODEL_NAME]: "synthetic-model", ...(placement === "creation" ? attributes : {}) },
          links: placement === "link" ? [{ context: { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 }, attributes }] : [],
        });
        if (placement === "setter") {
          span.setAttributes(attributes);
          for (const [key, value] of Object.entries(attributes)) span.setAttribute(key, value);
        }
        span.end();
        const finished = exporter.getFinishedSpans()[0];
        expect(JSON.stringify({ attributes: finished.attributes, links: finished.links })).not.toContain("SYNTHETIC-PRIVATE-PAYLOAD");
        expect(finished.attributes[SC.LLM_MODEL_NAME]).toBe("synthetic-model");
      });
    }
  }

  it("preserves all payloads with hiding disabled", () => {
    const attributes = Object.fromEntries(cases.flatMap(([, keys]) => keys.map(key => [key, "SYNTHETIC-PUBLIC-PAYLOAD"])));
    const tracer = new FITracer({ tracer: provider.getTracer("privacy"), traceConfig: {} });
    const span = tracer.startSpan("test", { attributes });
    span.end();
    expect(exporter.getFinishedSpans()[0].attributes).toEqual(attributes);
  });
});
