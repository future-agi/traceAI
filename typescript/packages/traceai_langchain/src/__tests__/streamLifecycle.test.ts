import { registerStreamRun, wrapModelStream } from "../streamLifecycle";
import { awaitAllCallbacks } from "@langchain/core/callbacks/promises";

describe("stream scope resource lifetime", () => {
  afterEach(() => { jest.useRealTimers(); jest.restoreAllMocks(); });

  it("allocates no listeners or timers before the first read", async () => {
    jest.useFakeTimers();
    const signal = new AbortController().signal;
    const add = jest.spyOn(signal, "addEventListener");
    const create = jest.fn(async function* () { yield 1; });
    const stream = wrapModelStream(create, "unused", signal, 200);
    expect(create).not.toHaveBeenCalled();
    expect(add).not.toHaveBeenCalled();
    expect(jest.getTimerCount()).toBe(0);
    await stream.return(undefined);
    expect(await stream.next()).toEqual({ done: true, value: undefined });
    expect(create).not.toHaveBeenCalled();
  });

  it.each(["complete", "return", "throw", "error"])("cleans timers on %s", async (action) => {
    jest.useFakeTimers();
    const error = new Error("synthetic");
    const stream = wrapModelStream(async function* () {
      yield 1;
      if (action === "error") throw error;
    }, `timer-${action}`, undefined, 200);
    await stream.next();
    expect(jest.getTimerCount()).toBe(1);
    if (action === "complete") await stream.next();
    if (action === "return") await stream.return(undefined);
    if (action === "throw") await expect(stream.throw(error)).rejects.toBe(error);
    if (action === "error") await expect(stream.next()).rejects.toBe(error);
    expect(jest.getTimerCount()).toBe(0);
  });

  it.each(["complete", "return", "throw", "error", "abort"])("removes the caller abort listener on %s", async (action) => {
    const controller = new AbortController();
    const add = jest.spyOn(controller.signal, "addEventListener");
    const remove = jest.spyOn(controller.signal, "removeEventListener");
    const error = new Error("synthetic");
    const stream = wrapModelStream(async function* () {
      yield 1;
      if (action === "error") throw error;
    }, `listener-${action}`, controller.signal);
    await stream.next();
    if (action === "complete") await stream.next();
    if (action === "return") await stream.return(undefined);
    if (action === "throw") await expect(stream.throw(error)).rejects.toBe(error);
    if (action === "error") await expect(stream.next()).rejects.toBe(error);
    if (action === "abort") { controller.abort(error); await stream.return(undefined); }
    expect(add).toHaveBeenCalledTimes(1);
    expect(remove).toHaveBeenCalledTimes(1);
    expect(remove).toHaveBeenCalledWith("abort", add.mock.calls[0][1]);
  });

  it("preserves a caller signal's precedence instead of manufacturing a deadline", async () => {
    jest.useFakeTimers();
    const controller = new AbortController();
    const stream = wrapModelStream(async function* (signal) {
      yield 1;
      signal.throwIfAborted();
      yield 2;
    }, "signal-precedence", controller.signal, 10);
    await stream.next();
    await jest.advanceTimersByTimeAsync(20);
    expect(await stream.next()).toEqual({ done: false, value: 2 });
    await stream.return(undefined);
    expect(jest.getTimerCount()).toBe(0);
  });

  it("aborts a pending read at the deadline and finalizes once", async () => {
    jest.useFakeTimers();
    const cancel = jest.fn(async () => {});
    const stream = wrapModelStream(async function* (signal) {
      registerStreamRun("deadline", cancel);
      yield 1;
      await new Promise((_, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true }));
    }, "deadline", undefined, 100);
    await stream.next();
    const pending = expect(stream.next()).rejects.toMatchObject({ name: "TimeoutError" });
    await jest.advanceTimersByTimeAsync(100);
    await pending;
    await stream.return(undefined);
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(jest.getTimerCount()).toBe(0);
  });

  it("includes background abort cleanup in LangChain's callback drain", async () => {
    const controller = new AbortController();
    let release!: () => void;
    const cleanup = new Promise<void>((resolve) => { release = resolve; });
    const cancel = jest.fn(async () => {});
    const stream = wrapModelStream(async function* () {
      registerStreamRun("drain", cancel);
      try { yield 1; } finally { await cleanup; }
    }, "drain", controller.signal);
    await stream.next();
    controller.abort();
    let drained = false;
    const drain = awaitAllCallbacks().then(() => { drained = true; });
    await Promise.resolve();
    expect(drained).toBe(false);
    release();
    await drain;
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("cleans up a synchronous iterator factory failure", async () => {
    const signal = new AbortController().signal;
    const remove = jest.spyOn(signal, "removeEventListener");
    const error = new Error("synthetic factory failure");
    const stream = wrapModelStream(() => { throw error; }, "factory-error", signal);
    await expect(stream.next()).rejects.toBe(error);
    expect(remove).toHaveBeenCalledTimes(1);
  });
});
