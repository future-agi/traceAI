import { context, trace, SpanStatusCode } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { CohereInstrumentation, isPatched } from "../instrumentation";

const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
let instrumentation: CohereInstrumentation;
beforeAll(() => context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable()));
beforeEach(() => {
  exporter.reset();
  instrumentation = new CohereInstrumentation();
  instrumentation.setTracerProvider(provider);
});
afterEach(() => instrumentation.disable());
afterAll(async () => { context.disable(); await provider.shutdown(); });

function instrument<T extends { prototype: object }>(CohereClient: T): T {
  instrumentation.manuallyInstrument({ CohereClient });
  return CohereClient;
}

it("does not flag an unsupported module as patched", () => {
  instrumentation.manuallyInstrument({});
  expect(isPatched()).toBe(false);
});

it("preserves synchronous errors and ends exactly one span", () => {
  const error = new Error("synchronous fixture failure");
  class Client { chat() { throw error; } }
  instrument(Client);
  expect(() => new Client().chat()).toThrow(error);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
});

it("does not double trace nested Cohere methods", async () => {
  class Client {
    async chat() { return this.embed(); }
    async embed() { return { embeddings: [[1]] }; }
  }
  instrument(Client);
  await new Client().chat();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

it("restores inherited methods without leaving shadow properties", () => {
  class Base { async chat() { return { text: "hello" }; } }
  class Client extends Base {}
  const original = Base.prototype.chat;
  instrument(Client);
  expect(Object.hasOwn(Client.prototype, "chat")).toBe(true);
  instrumentation.disable();
  expect(Object.hasOwn(Client.prototype, "chat")).toBe(false);
  expect(Client.prototype.chat).toBe(original);
});

it("keeps SDK iterator work in the span context, but not consumer work", async () => {
  const active: (string | undefined)[] = [];
  let controller: AbortController;
  class Client {
    async chatStream() {
      active.push(trace.getActiveSpan()?.spanContext().spanId);
      controller = new AbortController();
      return {
        controller,
        async *[Symbol.asyncIterator]() {
          active.push(trace.getActiveSpan()?.spanContext().spanId);
          yield { eventType: "text-generation", text: "hello" };
          await Promise.resolve();
          active.push(trace.getActiveSpan()?.spanContext().spanId);
        },
      };
    }
  }
  instrument(Client);
  for await (const _event of await new Client().chatStream()) {
    expect(trace.getActiveSpan()).toBeUndefined();
  }
  const [span] = exporter.getFinishedSpans();
  expect(active).toEqual(Array(3).fill(span.spanContext().spanId));
  controller!.abort();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

it("forwards iterator.throw and preserves error identity", async () => {
  const failure = new Error("iterator failure");
  const cleanup = jest.fn();
  class Client {
    async chatStream() {
      return {
        async *[Symbol.asyncIterator]() {
          try { yield { eventType: "text-generation", text: "partial" }; }
          finally { cleanup(); }
        },
      };
    }
  }
  instrument(Client);
  const stream = await new Client().chatStream();
  const iterator = stream[Symbol.asyncIterator]();
  await iterator.next();
  await expect(iterator.throw(failure)).rejects.toBe(failure);
  expect(cleanup).toHaveBeenCalledTimes(1);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
});

it("marks terminal error events without changing their payload", async () => {
  const event = { eventType: "stream-end", finishReason: "ERROR", response: { text: "partial" } };
  class Client { async chatStream() { return { async *[Symbol.asyncIterator]() { yield event; } }; } }
  instrument(Client);
  const values = [];
  for await (const item of await new Client().chatStream()) values.push(item);
  expect(values[0]).toBe(event);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
});

it("keeps v1 usable if v2 discovery fails", async () => {
  class Client {
    get v2() { throw new Error("unsupported getter"); }
    async chat() { return { text: "hello" }; }
  }
  instrument(Client);
  await new Client().chat();
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

it("ends a span when iterator creation itself fails", async () => {
  const failure = new Error("iterator creation failed");
  class Client {
    async chatStream() {
      return { [Symbol.asyncIterator](): AsyncIterator<unknown> { throw failure; } };
    }
  }
  instrument(Client);
  const stream = await new Client().chatStream();
  expect(() => stream[Symbol.asyncIterator]()).toThrow(failure);
  expect(exporter.getFinishedSpans()).toHaveLength(1);
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
});

it("marks an undefined rejection as an error without changing it", async () => {
  class Client { chat() { return Promise.reject(undefined); } }
  instrument(Client);
  await expect(new Client().chat()).rejects.toBeUndefined();
  expect(exporter.getFinishedSpans()[0].status.code).toBe(SpanStatusCode.ERROR);
});
