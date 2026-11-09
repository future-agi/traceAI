import { SpanStatusCode } from "@opentelemetry/api";
import { createRequire } from "node:module";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import * as pinecone from "@pinecone-database/pinecone";
import { PineconeInstrumentation, isPatched } from "../instrumentation";

class TestInstrumentation extends PineconeInstrumentation {
  moduleDefinition() {
    return this.init();
  }
}

// Resolve the selected real SDK, including Jest's compatibility-matrix mapper.
// Object arguments arrived in SDK 7, before the searchDocuments API in SDK 9.
const sdkVersion = createRequire(require.resolve("@pinecone-database/pinecone"))("../package.json").version;
const usesObjectArguments = Number(sdkVersion.split(".")[0]) >= 7;

const methods = [
  "query", "upsert", "fetch", "update", "deleteOne", "deleteMany",
  "deleteAll", "listPaginated", "describeIndexStats",
] as const;
const originals = new Map(methods
  .filter(method => typeof pinecone.Index.prototype[method] === "function")
  .map(method => [method, pinecone.Index.prototype[method]]));

// The real SDK still validates and serializes requests; only its HTTP transport
// is replaced. No Pinecone service or credentials are used.
function createIndex() {
  const fetchApi = jest.fn(async () => new Response(JSON.stringify({
    matches: [{ id: "record", score: 0.9 }],
    vectors: [],
    dimension: 2,
    totalRecordCount: 1,
  }), { status: 200, headers: { "content-type": "application/json" } }));
  const Index = pinecone.Index as any;
  const config = { apiKey: "offline-test-key", fetchApi };
  const index: pinecone.Index = usesObjectArguments
    ? new Index({ name: "test-index", namespace: "test-namespace", host: "https://pinecone.invalid" }, config)
    : new Index("test-index", config, "test-namespace", "https://pinecone.invalid");
  return { index, fetchApi };
}

async function callOperations(index: pinecone.Index) {
  const modern = usesObjectArguments;
  await index.query({ vector: [0.1, 0.2], topK: 1 });
  if (modern) {
    await (index as any).upsert({ records: [{ id: "record", values: [0.1, 0.2] }, { id: "other", values: [0.3, 0.4] }] });
    await (index as any).fetch({ ids: ["record", "other"] });
  } else {
    await index.upsert([{ id: "record", values: [0.1, 0.2] }]);
    await index.fetch(["record"]);
  }
  await index.update({ id: "record", metadata: { category: "test" } });
  await (index as any).deleteOne(modern ? { id: "record" } : "record");
  await (index as any).deleteMany(modern ? { ids: ["record", "other"], namespace: "override" } : ["record"]);
  await index.deleteAll();
  // Added after the supported 2.0.0 floor.
  if (typeof index.listPaginated === "function") await index.listPaginated({ limit: 1 });
  await index.describeIndexStats();
}

describe("Pinecone lifecycle with the real SDK and tracer", () => {
  let instrumentation: TestInstrumentation;
  let provider: BasicTracerProvider;
  let exporter: InMemorySpanExporter;
  const extraInstrumentations: PineconeInstrumentation[] = [];

  beforeEach(() => {
    exporter = new InMemorySpanExporter();
    provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    instrumentation = new TestInstrumentation({ instrumentationConfig: { enabled: false } });
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterEach(async () => {
    for (const extra of extraInstrumentations.splice(0)) extra.disable();
    instrumentation.disable();
    instrumentation.moduleDefinition().unpatch!(pinecone);
    // Restore test isolation even when running against the broken baseline.
    for (const [method, original] of originals) {
      Object.defineProperty(pinecone.Index.prototype, method, { value: original, writable: true, configurable: true });
    }
    await provider.shutdown();
  });

  it("exports one, zero, then one span across module patch/unpatch/repatch", async () => {
    const definition = instrumentation.moduleDefinition();
    const { index } = createIndex();
    const counts: number[] = [];
    definition.patch!(pinecone);
    for (let phase = 0; phase < 3; phase++) {
      exporter.reset();
      await index.query({ vector: [0.1, 0.2], topK: 1 });
      await provider.forceFlush();
      counts.push(exporter.getFinishedSpans().length);
      if (phase === 0) definition.unpatch!(pinecone);
      if (phase === 1) definition.patch!(pinecone);
    }
    expect(counts).toEqual([1, 0, 1]);
  });

  it("restores every original method on disable and retraces manual modules on enable", async () => {
    const { index, fetchApi } = createIndex();
    instrumentation.manuallyInstrument(pinecone);
    await callOperations(index);
    expect(exporter.getFinishedSpans()).toHaveLength(originals.size);
    instrumentation.disable();
    for (const [method, original] of originals) expect(pinecone.Index.prototype[method]).toBe(original);
    expect(isPatched()).toBe(false);
    exporter.reset();
    await callOperations(index);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    instrumentation.enable();
    instrumentation.enable();
    await callOperations(index);
    expect(exporter.getFinishedSpans()).toHaveLength(originals.size);
    expect(fetchApi).toHaveBeenCalledTimes(originals.size * 3);
  });

  it("does not duplicate spans on repeated manual registration", async () => {
    instrumentation.manuallyInstrument(pinecone);
    const first = pinecone.Index.prototype.query;
    instrumentation.manuallyInstrument(pinecone);
    expect(pinecone.Index.prototype.query).toBe(first);
    await createIndex().index.query({ vector: [0.1, 0.2], topK: 1 });
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("does not mark missing operation surfaces as patched or block real modules", async () => {
    instrumentation.manuallyInstrument({});
    instrumentation.manuallyInstrument({ Index: { prototype: {} } });
    expect(isPatched()).toBe(false);
    instrumentation.manuallyInstrument(pinecone);
    await createIndex().index.query({ vector: [0.1, 0.2], topK: 1 });
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("tracks separate module copies independently", async () => {
    class OtherIndex {
      async query() { return { matches: [] }; }
    }
    const original = OtherIndex.prototype.query;
    instrumentation.manuallyInstrument(pinecone);
    instrumentation.manuallyInstrument({ Index: OtherIndex });
    await new OtherIndex().query();
    await createIndex().index.query({ vector: [0.1, 0.2], topK: 1 });
    expect(exporter.getFinishedSpans()).toHaveLength(2);
    instrumentation.disable();
    expect(OtherIndex.prototype.query).toBe(original);
  });

  it("does not let another instance remove or duplicate the owner's wrappers", async () => {
    const other = new TestInstrumentation({ instrumentationConfig: { enabled: false } });
    other.setTracerProvider(provider);
    other.enable();
    extraInstrumentations.push(other);
    instrumentation.manuallyInstrument(pinecone);
    other.manuallyInstrument(pinecone);
    other.moduleDefinition().unpatch!(pinecone);
    expect(isPatched()).toBe(true);
    await createIndex().index.query({ vector: [0.1, 0.2], topK: 1 });
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    instrumentation.disable();
    expect(isPatched()).toBe(false);
    other.manuallyInstrument(pinecone);
    await createIndex().index.query({ vector: [0.1, 0.2], topK: 1 });
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  });

  it("leaves later wrappers intact and makes stale wrappers inert across re-enable", async () => {
    const { index } = createIndex();
    instrumentation.manuallyInstrument(pinecone);
    const cachedQuery = index.query.bind(index);
    const replacement = jest.fn(cachedQuery);
    pinecone.Index.prototype.query = replacement;
    instrumentation.disable();
    expect(pinecone.Index.prototype.query).toBe(replacement);
    await cachedQuery({ vector: [0.1, 0.2], topK: 1 });
    await index.query({ vector: [0.1, 0.2], topK: 1 });
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    instrumentation.enable();
    await index.query({ vector: [0.1, 0.2], topK: 1 });
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(replacement).toHaveBeenCalledTimes(2);
    instrumentation.disable();
    expect(pinecone.Index.prototype.query).toBe(replacement);
  });

  it("records latest SDK object arguments and forwards optional operation options", async () => {
    if (!usesObjectArguments) return;
    const { index, fetchApi } = createIndex();
    instrumentation.manuallyInstrument(pinecone);
    await callOperations(index);
    const spans = exporter.getFinishedSpans();
    expect(spans.find(span => span.name === "pinecone upsert")?.attributes).toMatchObject({
      "db.vector.upsert.count": 2,
      "db.vector.upsert.dimensions": 2,
    });
    expect(spans.find(span => span.name === "pinecone fetch")?.attributes["db.vector.fetch.ids_count"]).toBe(2);
    expect(spans.find(span => span.name === "pinecone delete_many")?.attributes).toMatchObject({
      "db.vector.delete.count": 2,
      "db.vector.namespace": "override",
    });
    exporter.reset();
    await (index as any).deleteOne({ id: "record", namespace: "override" });
    expect(exporter.getFinishedSpans()[0].attributes["db.vector.namespace"]).toBe("override");
    exporter.reset();
    const filter = { category: { $eq: "test" } };
    await (index as any).describeIndexStats({ filter });
    const statsRequest = fetchApi.mock.calls[fetchApi.mock.calls.length - 1] as any[];
    expect(JSON.parse(statsRequest[1].body).filter).toEqual(filter);
    exporter.reset();
    await (index as any).deleteAll({ namespace: "override" });
    expect(exporter.getFinishedSpans()[0].attributes["db.vector.namespace"]).toBe("override");
    const lastRequest = fetchApi.mock.calls[fetchApi.mock.calls.length - 1] as any[];
    expect(JSON.parse(lastRequest[1].body).namespace).toBe("override");
  });

  it("records the index and namespace from real SDK state", async () => {
    const { index } = createIndex();
    instrumentation.manuallyInstrument(pinecone);
    await index.namespace("child-namespace").query({ vector: [0.1, 0.2], topK: 1 });
    expect(exporter.getFinishedSpans()[0].attributes).toMatchObject({
      "db.vector.index.name": "test-index",
      "db.vector.namespace": "child-namespace",
      "db.namespace": "child-namespace",
    });
  });

  it("preserves the SDK rejection and ends exactly one error span", async () => {
    const { index, fetchApi } = createIndex();
    fetchApi.mockImplementation(async () => new Response("invalid query", { status: 400 }));
    instrumentation.manuallyInstrument(pinecone);
    await expect(index.query({ vector: [0.1, 0.2], topK: 1 })).rejects.toThrow();
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
  });
});
