import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { QdrantInstrumentation, isPatched } from "../instrumentation";

// Real OTel + FITracer; only the client transport is synthetic.
describe("Qdrant patch lifecycle", () => {
  let exporter: InMemorySpanExporter;
  let provider: BasicTracerProvider;
  let instrumentation: QdrantInstrumentation;

  const clientModule = () => ({
    QdrantClient: class {
      async query(_collection: string, _params: unknown) { return { points: [] }; }
    },
  });

  beforeEach(() => {
    exporter = new InMemorySpanExporter();
    provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    instrumentation = new QdrantInstrumentation({ instrumentationConfig: { enabled: false } });
    instrumentation.setTracerProvider(provider);
    instrumentation.enable();
  });

  afterEach(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  it("restores originals on disable and emits one span after repeated re-enable", async () => {
    const module = clientModule();
    const original = module.QdrantClient.prototype.query;
    const client = new module.QdrantClient();
    instrumentation.manuallyInstrument(module);
    instrumentation.manuallyInstrument(module);
    await client.query("documents", {});
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    for (let cycle = 0; cycle < 3; cycle++) {
      instrumentation.disable();
      expect(module.QdrantClient.prototype.query).toBe(original);
      expect(isPatched()).toBe(false);
      exporter.reset();
      await client.query("documents", {});
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      instrumentation.enable();
      instrumentation.enable();
      await client.query("documents", {});
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    }
  });

  it("patches each module copy independently and restores both", async () => {
    const modules = [clientModule(), clientModule()];
    const originals = modules.map(module => module.QdrantClient.prototype.query);
    for (const module of modules) {
      instrumentation.manuallyInstrument(module);
      await new module.QdrantClient().query("documents", {});
    }
    expect(exporter.getFinishedSpans()).toHaveLength(2);
    instrumentation.disable();
    modules.forEach((module, i) => expect(module.QdrantClient.prototype.query).toBe(originals[i]));
    instrumentation.enable();
    exporter.reset();
    for (const module of modules) await new module.QdrantClient().query("documents", {});
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  });

  it("does not let a non-owner unwrap or double-wrap the same prototype", async () => {
    const module = clientModule();
    instrumentation.manuallyInstrument(module);
    const wrapper = module.QdrantClient.prototype.query;
    const other = new QdrantInstrumentation();
    try {
      other.setTracerProvider(provider);
      other.manuallyInstrument(module);
      expect(module.QdrantClient.prototype.query).toBe(wrapper);
      other.disable();
      expect(module.QdrantClient.prototype.query).toBe(wrapper);
      await new module.QdrantClient().query("documents", {});
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    } finally { other.disable(); }
  });

  it("does not patch while disabled or claim an empty module", async () => {
    instrumentation.manuallyInstrument({});
    expect(isPatched()).toBe(false);
    instrumentation.disable();
    const module = clientModule();
    const original = module.QdrantClient.prototype.query;
    instrumentation.manuallyInstrument(module);
    expect(module.QdrantClient.prototype.query).toBe(original);
    instrumentation.enable();
    await new module.QdrantClient().query("documents", {});
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("preserves a later wrapper and leaves its captured old wrapper inactive", async () => {
    const module = clientModule();
    instrumentation.manuallyInstrument(module);
    const captured = module.QdrantClient.prototype.query;
    const outer = jest.fn(function (this: InstanceType<typeof module.QdrantClient>, ...args: Parameters<typeof captured>) {
      return captured.apply(this, args);
    });
    module.QdrantClient.prototype.query = outer;
    instrumentation.disable();
    expect(module.QdrantClient.prototype.query).toBe(outer);
    await new module.QdrantClient().query("documents", {});
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    instrumentation.enable();
    await new module.QdrantClient().query("documents", {});
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(outer).toHaveBeenCalledTimes(2);
  });

  it("refreshes FITracer when a provider is installed after enable", async () => {
    instrumentation.disable();
    instrumentation = new QdrantInstrumentation();
    instrumentation.setTracerProvider(provider);
    const module = clientModule();
    instrumentation.manuallyInstrument(module);
    await new module.QdrantClient().query("documents", {});
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("preserves receiver, all arguments, result identity, and rejection identity", async () => {
    const result = { points: [{ id: 1 }] };
    const error = new Error("synthetic query failure");
    const original = jest.fn(async function (this: unknown, ..._args: unknown[]) { return result; });
    const module = { QdrantClient: { prototype: { query: original } } };
    const receiver = {};
    const args = ["documents", { query: [0.1] }, { futureOption: true }];
    instrumentation.manuallyInstrument(module);
    expect(await module.QdrantClient.prototype.query.apply(receiver, args)).toBe(result);
    expect(original.mock.instances[0]).toBe(receiver);
    expect(original).toHaveBeenCalledWith(...args);
    original.mockRejectedValueOnce(error);
    await expect(module.QdrantClient.prototype.query.apply(receiver, args)).rejects.toBe(error);
    expect(exporter.getFinishedSpans()).toHaveLength(2);
    expect(exporter.getFinishedSpans()[1].status.code).toBe(2);
  });
});
