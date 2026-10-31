import { EventEmitter } from "node:events";
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
