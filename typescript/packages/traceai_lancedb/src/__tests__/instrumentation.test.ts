import { context, trace, SpanStatusCode } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { LanceDBInstrumentation, isPatched } from "../instrumentation";

// This synthetic engine deliberately mirrors LanceDB's lazy public surface.
// Native-engine compatibility is tested separately in execution.test.ts.
describe("LanceDB instrumentation lifecycle", () => {
  let exporter: InMemorySpanExporter;
  let provider: BasicTracerProvider;
  let instrumentation: LanceDBInstrumentation;
  let module: any;
  let table: any;
  let query: any;
  let iterator: any;
  let activeSpans: (string | undefined)[];

  beforeAll(() => context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable()));
  afterAll(() => context.disable());
  beforeEach(async () => {
    exporter = new InMemorySpanExporter();
    provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    instrumentation = new LanceDBInstrumentation();
    instrumentation.setTracerProvider(provider);
    activeSpans = [];
    iterator = {
      next: jest.fn(async () => {
        activeSpans.push(trace.getActiveSpan()?.spanContext().spanId);
        return { done: false, value: { numRows: 2 } };
      }),
      return: jest.fn(async (value) => ({ done: true, value })),
      throw: jest.fn(async (error) => { throw error; }),
    };
    class Query {
      limit() { return this; }
      execute() { return iterator; }
      [Symbol.asyncIterator]() { return this.execute(); }
      async toArrow() { const it = this.execute(); return (await it.next()).value; }
      async toArray() { await this.toArrow(); return [{ private: "payload" }]; }
    }
    class Table {
      name = "vectors";
      search() { return query = new Query(); }
      async countRows() { return 2; }
    }
    table = new Table();
    class Connection { async openTable() { return table; } }
    module = { connect: async () => new Connection() };
    instrumentation.manuallyInstrument(module);
    const db = await module.connect("not-captured");
    await db.openTable("vectors");
    exporter.reset();
  });
  afterEach(async () => {
    instrumentation.disable();
    await provider.shutdown();
  });

  it("emits one execution span rather than spans for nested terminal methods", async () => {
    const parent = provider.getTracer("test").startSpan("parent");
    await context.with(trace.setSpan(context.active(), parent), () => table.search([1, 2]).limit(1).toArray());
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].parentSpanContext?.spanId).toBe(parent.spanContext().spanId);
    expect(activeSpans).toEqual([spans[0].spanContext().spanId]);
    expect(spans[0].attributes["db.vector.query.top_k"]).toBe(1);
    expect(JSON.stringify(spans.map(span => span.attributes))).not.toContain("payload");
    parent.end();
  });

  it("keeps iterator identity, context and lifecycle through completion", async () => {
    const result = table.search([1, 2]).execute();
    expect(result).toBe(iterator);
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    await result.next();
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    await result.return("finished");
    const spans = exporter.getFinishedSpans();
    expect(spans).toHaveLength(1);
    expect(spans[0].attributes["db.response.returned_rows"]).toBe(2);
    expect(activeSpans).toEqual([spans[0].spanContext().spanId]);
    await result.return();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("closes spans on implicit return when an iterator has no cleanup method", async () => {
    delete iterator.return;
    for await (const batch of table.search([1, 2])) {
      expect(batch.numRows).toBe(2);
      break;
    }
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("records asynchronous iterator failure and rethrows the original error", async () => {
    const error = new Error("engine failure");
    iterator.next.mockRejectedValueOnce(error);
    const result = table.search([1, 2]).execute();
    await expect(result.next()).rejects.toBe(error);
    await result.return();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
  });

  it("forwards throw and records cleanup failure", async () => {
    const error = new Error("consumer failure");
    const result = table.search([1, 2]).execute();
    await expect(result.throw(error)).rejects.toBe(error);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
    exporter.reset();
    iterator.return = jest.fn().mockRejectedValueOnce(error);
    await expect(table.search([1, 2]).execute().return()).rejects.toBe(error);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
  });

  it("preserves synchronous errors at execution", () => {
    const error = new Error("sync failure");
    const q = table.search([1, 2]);
    const prototype = Object.getPrototypeOf(q);
    instrumentation.disable();
    prototype.execute = () => { throw error; };
    instrumentation.enable();
    table.search([1, 2]); // discover the replacement terminal
    expect(() => q.execute()).toThrow(error);
    expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
  });

  it("restores all owned methods and re-enables existing instances", async () => {
    const wrappedCount = table.countRows;
    const q = table.search([1, 2]);
    const wrappedExecute = q.execute;
    instrumentation.disable();
    expect(isPatched()).toBe(false);
    expect(table.countRows).not.toBe(wrappedCount);
    expect(q.execute).not.toBe(wrappedExecute);
    await table.countRows();
    expect(exporter.getFinishedSpans()).toHaveLength(0);
    instrumentation.enable();
    expect(table.countRows).toBe(wrappedCount);
    expect(q.execute).toBe(wrappedExecute);
    await table.countRows();
    expect(exporter.getFinishedSpans()).toHaveLength(1);
  });

  it("does not double-wrap or let a second owner disable the first", async () => {
    const second = new LanceDBInstrumentation();
    second.setTracerProvider(provider);
    try {
      second.manuallyInstrument(module);
      instrumentation.manuallyInstrument(module);
      second.disable();
      await table.countRows();
      expect(exporter.getFinishedSpans()).toHaveLength(1);
    } finally { second.disable(); }
  });

  it("warns instead of reporting successful instrumentation for unsupported exports", () => {
    instrumentation.disable();
    const fresh = new LanceDBInstrumentation();
    const warn = jest.spyOn((fresh as any)._diag, "warn").mockImplementation(() => {});
    try {
      fresh.manuallyInstrument({ Connection: class {}, Table: class {} });
      expect(isPatched()).toBe(false);
      expect(warn).toHaveBeenCalled();
    } finally { fresh.disable(); warn.mockRestore(); }
  });
});
