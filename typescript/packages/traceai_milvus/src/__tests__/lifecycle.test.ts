import { context, trace, SpanStatusCode } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import {
  BasicTracerProvider,
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { MilvusInstrumentation, isPatched } from "../instrumentation";

const params = { collection_name: "synthetic", limit: 2 };
const makeModule = () => {
  class Data {
    async search(_params: unknown) { return { results: [{ id: 1 }] }; }
  }
  class MilvusClient extends Data {
    hybridSearch = this.search;
  }
  return { MilvusClient };
};

describe("Milvus patch lifecycle with real OpenTelemetry", () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
  });
  const contextManager = new AsyncLocalStorageContextManager();
  let instruments: MilvusInstrumentation[];
  const instrument = () => {
    const instance = new MilvusInstrumentation();
    instance.setTracerProvider(provider);
    instruments.push(instance);
    return instance;
  };

  beforeAll(() => {
    context.setGlobalContextManager(contextManager.enable());
    trace.setGlobalTracerProvider(provider);
  });
  beforeEach(() => { instruments = []; exporter.reset(); });
  afterEach(() => {
    instruments.forEach(instance => instance.disable());
    expect(isPatched()).toBe(false);
  });
  afterAll(async () => { context.disable(); await provider.shutdown(); });

  it("restores inherited methods and disables aliases captured during construction", async () => {
    const module = makeModule();
    const original = module.MilvusClient.prototype.search;
    const instance = instrument();
    instance.manuallyInstrument(module);
    const client = new module.MilvusClient();
    await client.search(params);
    await client.hybridSearch(params);
    expect(exporter.getFinishedSpans()).toHaveLength(2);
    instance.disable();
    expect(module.MilvusClient.prototype.search).toBe(original);
    expect(Object.prototype.hasOwnProperty.call(module.MilvusClient.prototype, "search")).toBe(false);
    exporter.reset();
    await client.search(params);
    await client.hybridSearch(params);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  });

  it("keeps one span per call across repeated enable/disable cycles, including saved aliases", async () => {
    const module = makeModule();
    const instance = instrument();
    instance.manuallyInstrument(module);
    const client = new module.MilvusClient();
    for (let cycle = 0; cycle < 3; cycle++) {
      instance.disable();
      instance.enable();
      instance.manuallyInstrument(module);
      exporter.reset();
      await client.search(params);
      await client.hybridSearch(params);
      expect(exporter.getFinishedSpans()).toHaveLength(2);
    }
  });

  it("restores and repatches through the loader hook callbacks", async () => {
    const module = makeModule();
    const instance = instrument();
    const definition = (instance as any).init();
    const original = module.MilvusClient.prototype.search;
    definition.patch(module, "3.0.6");
    const client = new module.MilvusClient();
    await client.search(params);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    definition.unpatch(module, "3.0.6");
    expect(module.MilvusClient.prototype.search).toBe(original);
    exporter.reset();
    await client.search(params);
    await client.hybridSearch(params);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    definition.patch(module, "3.0.6");
    await client.search(params);
    await client.hybridSearch(params);
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  });

  it("instruments distinct SDK module copies independently", async () => {
    const first = makeModule();
    const second = makeModule();
    const instance = instrument();
    instance.manuallyInstrument(first);
    instance.manuallyInstrument(second);
    await new first.MilvusClient().search(params);
    await new second.MilvusClient().search(params);
    expect(exporter.getFinishedSpans()).toHaveLength(2);
  });

  it("does not mark an unsupported module as patched or block a subsequent real module", async () => {
    const instance = instrument();
    instance.manuallyInstrument({});
    expect(isPatched()).toBe(false);
    const module = makeModule();
    instance.manuallyInstrument(module);
    await new module.MilvusClient().search(params);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("shares a prototype without double wrapping or disabling another owner", async () => {
    const module = makeModule();
    const original = module.MilvusClient.prototype.search;
    const first = instrument();
    const second = instrument();
    first.manuallyInstrument(module);
    const wrapper = module.MilvusClient.prototype.search;
    second.manuallyInstrument(module);
    expect(module.MilvusClient.prototype.search).toBe(wrapper);
    first.disable();
    expect(isPatched()).toBe(true);
    await new module.MilvusClient().search(params);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    second.disable();
    expect(module.MilvusClient.prototype.search).toBe(original);
  });

  it("does not overwrite a wrapper installed later by another library", async () => {
    const module = makeModule();
    const instance = instrument();
    instance.manuallyInstrument(module);
    const wrapped = module.MilvusClient.prototype.search;
    const foreign = jest.fn(function (this: unknown, value: unknown) {
      return wrapped.call(this, value);
    });
    module.MilvusClient.prototype.search = foreign;
    instance.disable();
    expect(module.MilvusClient.prototype.search).toBe(foreign);
    await new module.MilvusClient().search(params);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    instance.enable();
    await new module.MilvusClient().search(params);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("repairs pre-existing aliases explicitly without double wrapping new aliases", async () => {
    const module = makeModule();
    const early = new module.MilvusClient();
    const instance = instrument();
    instance.manuallyInstrument(module);
    // Module instrumentation cannot discover instances created before setup.
    await early.hybridSearch(params);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    const late = new module.MilvusClient();
    instance.manuallyInstrumentClient(early);
    instance.manuallyInstrumentClient(late);
    for (let cycle = 0; cycle < 3; cycle++) {
      exporter.reset();
      await early.search(params);
      await early.hybridSearch(params);
      await late.search(params);
      await late.hybridSearch(params);
      expect(exporter.getFinishedSpans()).toHaveLength(4);
      instance.disable();
      exporter.reset();
      await early.hybridSearch(params);
      await late.hybridSearch(params);
      expect(exporter.getFinishedSpans()).toHaveLength(0);
      instance.enable();
    }
  });

  it("defers manual instrumentation while disabled", async () => {
    const instance = new MilvusInstrumentation({ instrumentationConfig: { enabled: false } });
    instruments.push(instance);
    const module = makeModule();
    instance.manuallyInstrument(module);
    const client = new module.MilvusClient();
    instance.manuallyInstrumentClient(client);
    await client.search(params);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    expect(isPatched()).toBe(false);
    instance.enable();
    await client.search(params);
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("does not throw or claim coverage for immutable methods", () => {
    const module = { MilvusClient: { prototype: Object.freeze({ search() {} }) } };
    instrument().manuallyInstrument(module);
    expect(isPatched()).toBe(false);
  });

  it("refreshes the tracer after an explicit provider is registered", async () => {
    const alternateExporter = new InMemorySpanExporter();
    const alternate = new BasicTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(alternateExporter)],
    });
    const instance = instrument();
    instance.setTracerProvider(alternate);
    const module = makeModule();
    instance.manuallyInstrument(module);
    await new module.MilvusClient().search(params);
    expect(alternateExporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    await alternate.shutdown();
  });

  it("restores an own property's exact descriptor", () => {
    const prototype = { search: async () => ({ results: [] }) };
    Object.defineProperty(prototype, "search", { enumerable: false });
    const original = Object.getOwnPropertyDescriptor(prototype, "search");
    const instance = instrument();
    instance.manuallyInstrument({ MilvusClient: { prototype } });
    instance.disable();
    expect(Object.getOwnPropertyDescriptor(prototype, "search")).toEqual(original);
  });

  it("waits for a pending operation and preserves its parent and return value", async () => {
    let finish!: (value: { results: number[] }) => void;
    const result = { results: [1, 2] };
    const prototype = {
      search: jest.fn(() => new Promise<{ results: number[] }>(resolve => { finish = resolve; })),
    };
    const instance = instrument();
    instance.manuallyInstrument({ MilvusClient: { prototype } });
    const parent = provider.getTracer("app").startSpan("parent");
    const pending = context.with(trace.setSpan(context.active(), parent), () => prototype.search());
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    instance.disable();
    finish(result);
    expect(await pending).toBe(result);
    const [span] = exporter.getFinishedSpans();
    expect(span.parentSpanContext?.spanId).toBe(parent.spanContext().spanId);
    expect(span.attributes["db.response.returned_rows"]).toBe(2);
    parent.end();
  });

  it("preserves rejection identity and ends one error span", async () => {
    const error = new Error("synthetic failure");
    const prototype = { search: () => Promise.reject(error) };
    const instance = instrument();
    instance.manuallyInstrument({ MilvusClient: { prototype } });
    await expect(prototype.search()).rejects.toBe(error);
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].status.code).toBe(SpanStatusCode.ERROR);
  });
});
