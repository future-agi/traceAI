import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { generateTraceConfig } from "@traceai/fi-core";
import { SemanticConventions as S } from "@traceai/fi-semantic-conventions";
import { outputAttributes } from "../attributes";
import { CohereInstrumentation } from "../instrumentation";

const captureConfig = generateTraceConfig({
  hideInputs: false, hideInputMessages: false, hideInputText: false, hideInputImages: false,
  hideOutputs: false, hideOutputMessages: false, hideOutputText: false,
  hideEmbeddingVectors: false, piiRedaction: false,
});
const vectorKey = (index: number) => `${S.EMBEDDING_EMBEDDINGS}.${index}.${S.EMBEDDING_VECTOR}`;
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
let instrumentation: CohereInstrumentation | undefined;
beforeEach(() => exporter.reset());
afterEach(() => instrumentation?.disable());
afterAll(async () => provider.shutdown());

describe("T8-TS-CLOUD-006 integer embedding attributes", () => {
  describe.each([
    { type: "int8", values: [[-128, 0, 127], [1, -2, 3]] },
    { type: "uint8", values: [[0, 128, 255], [1, 2, 3]] },
  ])("$type", ({ type, values }) => {
    const vectors = Object.freeze(values.map(vector => Object.freeze(vector)));

    it.each([false, true])("exports every vector through the wrapped SDK without changing the response (v2=%s)", async v2 => {
      const response = Object.freeze({ embeddings: Object.freeze({ [type]: vectors }) });
      const request = Object.freeze({
        model: "embed-english-v3.0", texts: ["Paris", "London"],
        inputType: "search_document", embeddingTypes: [type],
      });
      const embed = jest.fn(async (_request: unknown) => response);
      // Fake the generated SDK's v1 method and v2 getter; neither performs I/O.
      class V2Client { embed(request: unknown) { return embed(request); } }
      class Client {
        embed(request: unknown) { return embed(request); }
        get v2() { return new V2Client(); }
      }
      instrumentation = new CohereInstrumentation({ traceConfig: captureConfig });
      instrumentation.setTracerProvider(provider);
      instrumentation.manuallyInstrument({ CohereClient: Client });
      const client = new Client();
      const result = await (v2 ? client.v2 : client).embed(request);

      expect(result).toBe(response);
      expect(result.embeddings[type]).toBe(vectors);
      expect(embed).toHaveBeenCalledTimes(1);
      expect(embed).toHaveBeenCalledWith(request);
      const spans = exporter.getFinishedSpans();
      expect(spans).toHaveLength(1);
      const attrs = spans[0].attributes;
      vectors.forEach((vector, index) => expect(attrs[vectorKey(index)]).toEqual(vector));
      expect(attrs["cohere.embedding_type"]).toBe(type);
      expect(JSON.parse(attrs[S.RAW_OUTPUT] as string)).toEqual(response);
    });

    it.each(["hideEmbeddingVectors", "hideOutputs", "hideOutputMessages", "hideOutputText"] as const)(
      "honors %s for standard and raw vectors",
      flag => {
        const attrs = outputAttributes("embed", { embeddings: { [type]: vectors } }, { ...captureConfig, [flag]: true });
        expect(attrs[vectorKey(0)]).toBeUndefined();
        expect(attrs[vectorKey(1)]).toBeUndefined();
        expect(attrs[S.RAW_OUTPUT]).toBeUndefined();
        expect(attrs["cohere.embedding_type"]).toBeUndefined();
      },
    );

    it("retains vectors when only input capture is hidden", () => {
      const response = { embeddings: { [type]: vectors }, texts: ["Paris", "London"] };
      const attrs = outputAttributes("embed", response, { ...captureConfig, hideInputs: true });
      vectors.forEach((vector, index) => expect(attrs[vectorKey(index)]).toEqual(vector));
      expect(JSON.parse(attrs[S.RAW_OUTPUT] as string)).toEqual({ embeddings: response.embeddings });
      expect(response.texts).toEqual(["Paris", "London"]);
    });
  });

  it("preserves legacy array embeddings", () => {
    const response = { embeddings: [[0.1, 0.2], [0.3, 0.4]] };
    const attrs = outputAttributes("embed", response, captureConfig);
    response.embeddings.forEach((vector, index) => expect(attrs[vectorKey(index)]).toEqual(vector));
    expect(JSON.parse(attrs[S.RAW_OUTPUT] as string)).toEqual(response);
  });

  it.each([
    { type: "float", embeddings: { float: [[0.1, 0.2]], int8: [[1, -2]], uint8: [[1, 2]] } },
    { type: "int8", embeddings: { int8: [[1, -2]], uint8: [[1, 2]], binary: [[-1]] } },
    { type: "uint8", embeddings: { float: null, uint8: [[1, 2]], ubinary: [[255]] } },
  ])("selects $type deterministically without mixing representations", ({ type, embeddings }) => {
    const attrs = outputAttributes("embed", { embeddings }, captureConfig);
    const expected = type === "float" ? [0.1, 0.2] : type === "int8" ? [1, -2] : [1, 2];
    expect(attrs[vectorKey(0)]).toEqual(expected);
    expect(attrs[vectorKey(1)]).toBeUndefined();
    expect(attrs["cohere.embedding_type"]).toBe(type);
    expect(JSON.parse(attrs[S.RAW_OUTPUT] as string)).toEqual({ embeddings });
  });

  it.each(["binary", "ubinary"])("does not reinterpret packed %s as coordinates", type => {
    const response = { embeddings: { [type]: [[1, 2, 3]] } };
    const attrs = outputAttributes("embed", response, captureConfig);
    expect(attrs[vectorKey(0)]).toBeUndefined();
    expect(attrs["cohere.embedding_type"]).toBeUndefined();
    expect(JSON.parse(attrs[S.RAW_OUTPUT] as string)).toEqual(response);
  });
});
