import { EventEmitter } from "events";
import { flushOnSignals, type SignalTarget } from "../index";

function fakeProcess() {
  const emitter = new EventEmitter();
  const kill = jest.fn();
  const target: SignalTarget = {
    pid: 4242,
    listeners: (event) => emitter.listeners(event),
    on: (event, listener) => emitter.on(event, listener),
    removeListener: (event, listener) => emitter.removeListener(event, listener as (...args: unknown[]) => void),
    kill,
  };
  return { emitter, target, kill };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

describe("flushOnSignals", () => {
  it("awaits flush before the earlier listeners (Genkit's exit handler) run", async () => {
    const { emitter, target } = fakeProcess();
    const order: string[] = [];
    // Stand-ins for genkit.ts `shutdown` (calls process.exit) and node-telemetry-provider `cleanUpTracing`.
    emitter.on("SIGTERM", () => order.push("genkit-exit"));
    emitter.on("SIGTERM", () => order.push("genkit-sdk-cleanup"));
    let release!: () => void;
    const flushing = new Promise<void>((resolve) => (release = resolve));
    flushOnSignals(
      async () => {
        order.push("flush-start");
        await flushing;
        order.push("flush-done");
      },
      { target },
    );
    expect(emitter.listenerCount("SIGTERM")).toBe(1);
    emitter.emit("SIGTERM", "SIGTERM");
    await tick();
    expect(order).toEqual(["flush-start"]);
    release();
    await tick();
    await tick();
    expect(order).toEqual(["flush-start", "flush-done", "genkit-exit", "genkit-sdk-cleanup"]);
  });

  it("still runs the earlier listeners when flush rejects or hangs", async () => {
    const { emitter, target } = fakeProcess();
    const seen: string[] = [];
    emitter.on("SIGTERM", () => seen.push("exit"));
    flushOnSignals(() => Promise.reject(new Error("collector down")), { target });
    emitter.emit("SIGTERM", "SIGTERM");
    await tick();
    await tick();
    expect(seen).toEqual(["exit"]);

    const second = fakeProcess();
    second.emitter.on("SIGINT", () => seen.push("int-exit"));
    flushOnSignals(() => new Promise(() => undefined), { target: second.target, timeoutMillis: 20, signals: ["SIGINT"] });
    second.emitter.emit("SIGINT", "SIGINT");
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(seen).toEqual(["exit", "int-exit"]);
  });

  it("re-raises the signal when nothing else was listening", async () => {
    const { emitter, target, kill } = fakeProcess();
    const flush = jest.fn(async () => undefined);
    flushOnSignals(flush, { target, signals: ["SIGTERM"] });
    emitter.emit("SIGTERM", "SIGTERM");
    await tick();
    await tick();
    expect(flush).toHaveBeenCalledTimes(1);
    expect(emitter.listenerCount("SIGTERM")).toBe(0);
    expect(kill).toHaveBeenCalledWith(4242, "SIGTERM");
  });

  it("flushes once even if the signal arrives twice", async () => {
    const { emitter, target } = fakeProcess();
    const flush = jest.fn(async () => undefined);
    emitter.on("SIGTERM", () => undefined);
    flushOnSignals(flush, { target });
    emitter.emit("SIGTERM", "SIGTERM");
    emitter.emit("SIGTERM", "SIGTERM");
    await tick();
    expect(flush).toHaveBeenCalledTimes(1);
  });

  it("uninstall restores the original listeners", () => {
    const { emitter, target } = fakeProcess();
    const original = () => undefined;
    emitter.on("SIGTERM", original);
    const uninstall = flushOnSignals(async () => undefined, { target });
    expect(emitter.listeners("SIGTERM")).not.toContain(original);
    uninstall();
    expect(emitter.listeners("SIGTERM")).toEqual([original]);
  });
});
