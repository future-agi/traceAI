import { EventEmitter } from "node:events";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { MongoDBInstrumentation, isPatched } from "../instrumentation";

// Lifecycle-only stand-in. Cursor, command, error and vector behavior is tested
// against real drivers in ../instrumentation.test.ts, not mocked collections.
describe("MongoDB instrumentation lifecycle", () => {
  let instrumentation: MongoDBInstrumentation;

  beforeEach(() => { instrumentation = new MongoDBInstrumentation(); });
  afterEach(() => { instrumentation.disable(); });

  test("retains its public name and starts enabled", () => {
    expect(instrumentation.instrumentationName).toBe("@traceai/mongodb");
    expect(instrumentation.isEnabled()).toBe(true);
    expect(isPatched()).toBe(false);
  });

  test("preserves connect promise identity, receiver and rejection", async () => {
    const failure = new Error("synthetic connect failure");
    const promise = Promise.reject(failure);
    class Client extends EventEmitter {
      monitorCommands = false;
      connect(): Promise<never> { return promise; }
    }
    const original = jest.fn(function (this: Client) {
      expect(this).toBe(client);
      return promise;
    });
    Client.prototype.connect = original;
    const client = new Client();
    instrumentation.manuallyInstrument({ MongoClient: Client });
    const result = client.connect();
    expect(result).toBe(promise);
    await expect(result).rejects.toBe(failure);
    expect(client.listenerCount("commandStarted")).toBe(0);
    expect(client.monitorCommands).toBe(false);
    instrumentation.disable();
    expect(Client.prototype.connect).toBe(original);
    expect(isPatched()).toBe(false);
  });

  test("a telemetry failure does not change connect behavior", async () => {
    const result = Promise.resolve("connected");
    class Client {
      get monitorCommands(): boolean { throw new Error("bad telemetry accessor"); }
      connect() { return result; }
    }
    instrumentation.manuallyInstrument({ MongoClient: Client });
    expect(new Client().connect()).toBe(result);
    await expect(result).resolves.toBe("connected");
  });
});

describe("MongoDB vector-only mode", () => {
  // Command events as the driver emits them on a client created with monitorCommands.
  class Client extends EventEmitter { monitorCommands = true; }
  function run(client: Client, requestId: number, commandName: string, command: Record<string, unknown>) {
    client.emit("commandStarted", { requestId, commandName, databaseName: "app", command });
    client.emit("commandSucceeded", { requestId, commandName, reply: { ok: 1 } });
  }

  test("traces only aggregate commands with a vector search stage", async () => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    // The option is new in this release; older type declarations do not list it.
    const instrumentation = new MongoDBInstrumentation({ instrumentationConfig: { vectorOnly: true } as any });
    instrumentation.setTracerProvider(provider);
    try {
      expect((instrumentation as any).vectorOnly).toBe(true);
      const client = new Client();
      instrumentation.instrumentClient(client as any);
      run(client, 1, "find", { find: "users", filter: { email: "person@example.com" } });
      run(client, 2, "insert", { insert: "orders", documents: [{ total: 1 }] });
      run(client, 3, "aggregate", { aggregate: "users", pipeline: [{ $match: { active: true } }] });
      run(client, 4, "aggregate", { aggregate: "docs", pipeline: [{ $vectorSearch: { index: "vectors", path: "embedding", queryVector: [0.1, 0.2], numCandidates: 10, limit: 2 } }] });
      const spans = exporter.getFinishedSpans();
      expect(spans.map((span) => span.name)).toEqual(["mongodb aggregate"]);
      expect(spans[0].attributes["db.operation.type"]).toBe("vector_search");
    } finally {
      instrumentation.disable();
      await provider.shutdown();
    }
  });

  test("traces every application command when the option is off", async () => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
    const instrumentation = new MongoDBInstrumentation();
    instrumentation.setTracerProvider(provider);
    try {
      const client = new Client();
      instrumentation.instrumentClient(client as any);
      run(client, 1, "find", { find: "users", filter: {} });
      run(client, 2, "aggregate", { aggregate: "users", pipeline: [{ $match: { active: true } }] });
      expect(exporter.getFinishedSpans().map((span) => span.name)).toEqual(["mongodb find", "mongodb aggregate"]);
    } finally {
      instrumentation.disable();
      await provider.shutdown();
    }
  });
});
