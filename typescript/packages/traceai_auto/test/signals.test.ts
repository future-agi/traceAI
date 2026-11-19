import process from "node:process";
import { chainSigterm } from "../src/signals";

// Jest supplies a cloned process/EventEmitter without Node's native signal hooks.
// Stub only that native binding boundary here; real OS delivery is exercised by e2e.
beforeEach(() => {
  const raw = process.rawListeners.bind(process);
  function startListeningIfSignal() {}
  function stopListeningIfSignal() {}
  jest.spyOn(process, "rawListeners").mockImplementation(event => event === "newListener" ? [startListeningIfSignal, ...raw(event)] : event === "removeListener" ? [stopListeningIfSignal, ...raw(event)] : raw(event));
});
afterEach(() => jest.restoreAllMocks());

test("signal gating waits for flush, preserves listener identity/order/once, and restores emit", async () => {
  const original = process.emit, calls: string[] = [];
  let entered!: () => void, release!: () => void, delivered!: () => void;
  const started = new Promise<void>(resolve => { entered = resolve; });
  const gate = new Promise<void>(resolve => { release = resolve; });
  const delivery = new Promise<void>(resolve => { delivered = resolve; });
  const first = () => calls.push("once"), second = () => { calls.push("on"); delivered(); };
  process.once("SIGTERM", first);
  const added = jest.fn(), removed = jest.fn();
  const onAdd = (name: string) => { if (name === "SIGTERM") added(); }, onRemove = (name: string) => { if (name === "SIGTERM") removed(); };
  process.on("newListener", onAdd); process.on("removeListener", onRemove);
  const remove = chainSigterm(async () => { calls.push("flush-start"); entered(); await gate; calls.push("flush-finished"); });
  expect(added).toHaveBeenCalledTimes(1); expect(removed).not.toHaveBeenCalled();
  process.removeListener("newListener", onAdd); process.removeListener("removeListener", onRemove);
  process.on("SIGTERM", second);
  try {
    expect(process.listeners("SIGTERM")).toContain(first); expect(process.listeners("SIGTERM")).toContain(second);
    process.emit("SIGTERM"); await started;
    expect(calls).toEqual(["flush-start"]);
    release(); await delivery;
    expect(calls).toEqual(["flush-start", "flush-finished", "once", "on"]);
    expect(process.listeners("SIGTERM")).not.toContain(first);
  } finally { remove(); process.removeListener("SIGTERM", first); process.removeListener("SIGTERM", second); }
  expect(process.emit).toBe(original);
});
test("non-signal events retain synchronous return values, arguments and thrown error identity", () => {
  const remove = chainSigterm(async () => {}), failure = new Error("application handler"), handler = jest.fn(() => { throw failure; });
  process.on("traceai-review-event", handler);
  try {
    expect(() => (process.emit as any)("traceai-review-event", 1, 2)).toThrow(failure);
    expect(handler).toHaveBeenCalledWith(1, 2);
    expect((process.emit as any)("traceai-review-absent")).toBe(false);
  } finally { remove(); process.removeListener("traceai-review-event", handler); }
});
test("a stalled signal flush releases handlers at the five-second deadline", async () => {
  jest.useFakeTimers({ doNotFake: ["setImmediate"] });
  let entered!: () => void, delivered!: () => void;
  const started = new Promise<void>(resolve => { entered = resolve; }), delivery = new Promise<void>(resolve => { delivered = resolve; });
  const handler = jest.fn(delivered); process.once("SIGTERM", handler);
  const remove = chainSigterm(() => { entered(); return new Promise<void>(() => {}); });
  try {
    process.emit("SIGTERM"); await started;
    jest.advanceTimersByTime(4999); expect(handler).not.toHaveBeenCalled();
    jest.advanceTimersByTime(1); await delivery; expect(handler).toHaveBeenCalledTimes(1);
  } finally { remove(); process.removeListener("SIGTERM", handler); jest.useRealTimers(); }
});
test("a rejected flush still delivers SIGTERM to the application", async () => {
  let delivered!: () => void;
  const delivery = new Promise<void>(resolve => { delivered = resolve; });
  const handler = jest.fn(delivered); process.once("SIGTERM", handler);
  const remove = chainSigterm(async () => { throw new Error("exporter failed"); });
  try { process.emit("SIGTERM"); await delivery; expect(handler).toHaveBeenCalledTimes(1); }
  finally { remove(); process.removeListener("SIGTERM", handler); }
});
