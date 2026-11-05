import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { QdrantInstrumentation } from "../instrumentation";

describe("modern Qdrant query operations", () => {
  let exporter: InMemorySpanExporter;
  let provider: BasicTracerProvider;
  let instrumentation: QdrantInstrumentation;

  beforeEach(() => {
    exporter = new InMemorySpanExporter();
    provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    instrumentation = new QdrantInstrumentation();
    instrumentation.setTracerProvider(provider);
  });

  afterEach(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  it.each([
    ["queryBatch", "query_batch", { searches: [{ query: [0.1] }, { query: [0.2] }] }, [{ points: [{ id: 1 }] }, { points: [{ id: 2 }, { id: 3 }] }], 3],
    ["queryGroups", "query_groups", { query: [0.1], group_by: "category" }, { groups: [{ hits: [{ id: 1 }, { id: 2 }] }, { hits: [{ id: 3 }] }] }, 3],
    ["queryBatch", "query_batch", { searches: [] }, [], 0],
    ["queryGroups", "query_groups", { query: [0.1], group_by: "category" }, { groups: [] }, 0],
    ["queryPoints", "query_points", { query: [0.1], limit: 0 }, { points: [] }, 0],
  ])("traces %s with its SDK response shape", async (method, operation, params, result, count) => {
    const original = jest.fn(async (..._args: unknown[]) => result);
    const prototype = { [method as string]: original };
    instrumentation.manuallyInstrument({ QdrantClient: { prototype } });
    const receiver = {};
    expect(await prototype[method as string].call(receiver, "documents", params)).toBe(result);
    expect(original.mock.instances[0]).toBe(receiver);
    expect(original).toHaveBeenCalledWith("documents", params);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].name).toBe(`qdrant ${operation}`);
    expect(spans[0].attributes["db.vector.results.count"]).toBe(count);
    expect(spans[0].attributes["db.operation.name"]).toBe(operation);
    expect(spans[0].status.code).toBe(1);
    instrumentation.disable();
    expect(prototype[method as string]).toBe(original);
  });

  it.each(["queryBatch", "queryGroups"])("preserves %s rejection and ends its error span", async method => {
    const error = new Error("synthetic failure");
    const original = jest.fn(async (..._args: unknown[]) => { throw error; });
    const prototype = { [method]: original };
    instrumentation.manuallyInstrument({ QdrantClient: { prototype } });
    await expect(prototype[method]("documents", {})).rejects.toBe(error);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(2);
    expect(exporter.getFinishedSpans()[0].events[0].name).toBe("exception");
  });
});
