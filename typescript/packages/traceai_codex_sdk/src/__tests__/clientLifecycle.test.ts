import { CodexSDKInstrumentation, isPatched } from "../index";
import { toolTurn } from "./fixtures/events";
import { fakeModule, scripted } from "./fixtures/fakeThread";
import { byName, memoryProvider } from "./helpers";

const methods = ["startThread", "resumeThread"] as const;

it.each(["registration", "reverse"])("restores both factories after two owners unpatch in %s order", async (order) => {
  const first = memoryProvider();
  const second = memoryProvider();
  const { Codex } = fakeModule();
  const client = new Codex([scripted(toolTurn()), scripted(toolTurn())]);
  const originals = methods.map((method) => client[method]);
  const a = new CodexSDKInstrumentation({ tracerProvider: first.provider });
  const b = new CodexSDKInstrumentation({ tracerProvider: second.provider });
  try {
    a.wrapCodex(client);
    b.wrapCodex(client);
    for (const owner of order === "registration" ? [a, b] : [b, a]) owner.unpatch();
    expect(methods.map((method) => client[method])).toEqual(originals);
    for (const method of methods) expect(Object.hasOwn(client, method)).toBe(false);
    expect(Object.getOwnPropertySymbols(client)).toEqual([]);
    await client.startThread().run("unpatched start");
    await client.resumeThread("resumed").run("unpatched resume");
    expect(first.exporter.getFinishedSpans()).toHaveLength(0);
    expect(second.exporter.getFinishedSpans()).toHaveLength(0);
  } finally {
    a.unpatch();
    b.unpatch();
    await first.provider.shutdown();
    await second.provider.shutdown();
  }
});

it("does not stack same-owner or different-owner factory patches", async () => {
  const first = memoryProvider();
  const second = memoryProvider();
  const { Codex } = fakeModule();
  const client = new Codex([scripted(toolTurn()), scripted(toolTurn())]);
  const a = new CodexSDKInstrumentation({ tracerProvider: first.provider });
  const b = new CodexSDKInstrumentation({ tracerProvider: second.provider });
  try {
    expect(a.wrapCodex(client)).toBe(client);
    const active = methods.map((method) => client[method]);
    a.wrapCodex(client);
    expect(methods.map((method) => client[method])).toEqual(active);
    b.wrapCodex(client);
    expect(methods.map((method) => client[method])).toEqual(active);
    b.unpatch();
    expect(methods.map((method) => client[method])).toEqual(active);
    await client.startThread().run("start");
    await client.resumeThread("resumed").run("resume");
    expect(byName(first.exporter.getFinishedSpans(), "codex.turn")).toHaveLength(2);
    expect(second.exporter.getFinishedSpans()).toHaveLength(0);
  } finally {
    a.unpatch();
    b.unpatch();
    await first.provider.shutdown();
    await second.provider.shutdown();
  }
});

it.each(methods)("preserves an application replacement of %s", (method) => {
  const { Codex } = fakeModule();
  const client = new Codex([]);
  const instrumentation = new CodexSDKInstrumentation();
  instrumentation.wrapCodex(client);
  const descriptor = { value: jest.fn(), writable: false, enumerable: true, configurable: true };
  Object.defineProperty(client, method, descriptor);
  instrumentation.unpatch();
  expect(Object.getOwnPropertyDescriptor(client, method)).toEqual(descriptor);
  expect(Object.getOwnPropertySymbols(client)).toEqual([]);
});

it.each(methods)("preserves an application accessor replacement of %s without invoking it", (method) => {
  const { Codex } = fakeModule();
  const client = new Codex([]);
  const instrumentation = new CodexSDKInstrumentation();
  instrumentation.wrapCodex(client);
  const getter = jest.fn(() => { throw new Error("must not be invoked during cleanup"); });
  const descriptor = { get: getter, enumerable: true, configurable: true };
  Object.defineProperty(client, method, descriptor);
  instrumentation.unpatch();
  expect(Object.getOwnPropertyDescriptor(client, method)).toEqual(descriptor);
  expect(getter).not.toHaveBeenCalled();
});

it.each(methods)("restores the full original own descriptor of %s", (method) => {
  const { Codex } = fakeModule();
  const client = new Codex([]);
  const descriptor = { value: client[method], writable: false, enumerable: true, configurable: true };
  Object.defineProperty(client, method, descriptor);
  const instrumentation = new CodexSDKInstrumentation();
  instrumentation.wrapCodex(client);
  instrumentation.unpatch();
  expect(Object.getOwnPropertyDescriptor(client, method)).toEqual(descriptor);
});

it.each(methods)("restores an original accessor descriptor of %s", (method) => {
  const { Codex } = fakeModule();
  const client = new Codex([]);
  const original = client[method];
  const descriptor = { get: () => original, enumerable: true, configurable: true };
  Object.defineProperty(client, method, descriptor);
  const instrumentation = new CodexSDKInstrumentation();
  instrumentation.wrapCodex(client);
  instrumentation.unpatch();
  expect(Object.getOwnPropertyDescriptor(client, method)).toEqual(descriptor);
});

it("keeps saved factories inert after unpatch, even when the owner is enabled again", async () => {
  const { provider, exporter } = memoryProvider();
  const { Codex } = fakeModule();
  const client = new Codex([scripted(toolTurn()), scripted(toolTurn())]);
  const instrumentation = new CodexSDKInstrumentation({ tracerProvider: provider });
  try {
    instrumentation.wrapCodex(client);
    const start = client.startThread;
    const resume = client.resumeThread;
    instrumentation.unpatch();
    const first = start.call(client);
    expect(isPatched(first)).toBe(false);
    await first.run("saved start");
    // Re-enabling the same instrumentation must not reactivate a saved wrapper.
    instrumentation.wrapCodex(new Codex([]));
    const second = resume.call(client, "resumed");
    expect(isPatched(second)).toBe(false);
    await second.run("saved resume");
    expect(exporter.getFinishedSpans()).toHaveLength(0);
  } finally {
    instrumentation.unpatch();
    await provider.shutdown();
  }
});

it("allows a fresh client owner without letting stale cleanup remove its factories", async () => {
  const first = memoryProvider();
  const second = memoryProvider();
  const { Codex } = fakeModule();
  const client = new Codex([scripted(toolTurn()), scripted(toolTurn())]);
  const a = new CodexSDKInstrumentation({ tracerProvider: first.provider });
  const b = new CodexSDKInstrumentation({ tracerProvider: second.provider });
  try {
    a.wrapCodex(client);
    const oldThread = client.startThread();
    a.unpatch();
    expect(isPatched(oldThread)).toBe(false);
    b.wrapCodex(client);
    a.unpatch();
    await client.startThread().run("new owner start");
    await client.resumeThread("resumed").run("new owner resume");
    expect(first.exporter.getFinishedSpans()).toHaveLength(0);
    expect(byName(second.exporter.getFinishedSpans(), "codex.turn")).toHaveLength(2);
  } finally {
    a.unpatch();
    b.unpatch();
    await first.provider.shutdown();
    await second.provider.shutdown();
  }
});

it("does not leave a client marker when no factories are available", () => {
  const instrumentation = new CodexSDKInstrumentation();
  const client = {};
  expect(instrumentation.wrapCodex(client)).toBe(client);
  expect(Reflect.ownKeys(client)).toEqual([]);
  instrumentation.unpatch();
});

it("rolls back a partial patch when the second factory cannot be replaced", () => {
  const { Codex } = fakeModule();
  const client = new Codex([]);
  Object.defineProperty(client, "resumeThread", { value: client.resumeThread, configurable: false, writable: false });
  const descriptors = Object.getOwnPropertyDescriptors(client);
  const instrumentation = new CodexSDKInstrumentation();
  try {
    expect(() => instrumentation.wrapCodex(client)).toThrow(TypeError);
    expect(Object.getOwnPropertyDescriptors(client)).toEqual(descriptors);
  } finally {
    instrumentation.unpatch();
  }
});

it("forwards factory receivers, arguments, results and errors unchanged", () => {
  const instrumentation = new CodexSDKInstrumentation();
  const result = {};
  const error = new Error("factory error");
  const startThread = jest.fn(function (this: unknown) { return result; });
  const resumeThread = jest.fn(() => { throw error; });
  const client = { startThread, resumeThread };
  const receiver = {};
  try {
    instrumentation.wrapCodex(client);
    expect(Reflect.apply(client.startThread, receiver, ["one", "two"])).toBe(result);
    expect(startThread.mock.contexts).toEqual([receiver]);
    expect(startThread.mock.calls).toEqual([["one", "two"]]);
    expect(() => client.resumeThread()).toThrow(error);
  } finally {
    instrumentation.unpatch();
  }
});
