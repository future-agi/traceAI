import { readFileSync } from "fs";
import { join } from "path";
import { CodexSDKInstrumentation, isPatched } from "../index";
import { usageBaselines } from "../spans";
import { toolTurn } from "./fixtures/events";
import { fakeModule, scripted } from "./fixtures/fakeThread";
import { byName, memoryProvider } from "./helpers";

beforeEach(() => usageBaselines.clear());

it("does not claim support for SDKs with unverified or per-turn usage semantics", () => {
  const manifest = JSON.parse(readFileSync(join(__dirname, "../../package.json"), "utf8"));
  // 0.44.0's real binary reports per-turn usage; 0.162.1 and 0.166.0 report
  // cumulative usage. The real-binary contract must pass at this declared floor.
  expect(manifest.peerDependencies["@openai/codex-sdk"]).toBe(">=0.162.1 <1");
});

it("restores instance methods and markers, then traces with a fresh provider", async () => {
  const first = memoryProvider();
  const second = memoryProvider();
  const mod = fakeModule();
  const thread = new mod.Codex([scripted(toolTurn()), scripted(toolTurn())]).startThread();
  const original = thread.runStreamedInternal;
  const a = new CodexSDKInstrumentation({ tracerProvider: first.provider });
  const b = new CodexSDKInstrumentation({ tracerProvider: second.provider });
  try {
    a.wrapThread(thread);
    await thread.run("first");
    a.unpatch();
    expect(isPatched(thread)).toBe(false);
    expect(Object.hasOwn(thread, "runStreamedInternal")).toBe(false);
    expect(thread.runStreamedInternal).toBe(original);
    b.wrapThread(thread);
    // A stale owner's repeated cleanup must not undo B's patch.
    a.unpatch();
    await thread.run("second");
    expect(byName(first.exporter.getFinishedSpans(), "codex.turn")).toHaveLength(1);
    expect(byName(second.exporter.getFinishedSpans(), "codex.turn")).toHaveLength(1);
  } finally {
    a.unpatch();
    b.unpatch();
  }
});

it("allows class instrumentation after an instance owner unpatches", async () => {
  const first = memoryProvider();
  const second = memoryProvider();
  const mod = fakeModule();
  const thread = new mod.Codex([scripted(toolTurn()), scripted(toolTurn())]).startThread();
  const a = new CodexSDKInstrumentation({ tracerProvider: first.provider });
  const b = new CodexSDKInstrumentation({ tracerProvider: second.provider });
  try {
    a.wrapThread(thread);
    await thread.run("first");
    a.unpatch();
    expect(b.manuallyInstrument(mod)).toBe(true);
    await thread.run("second");
    expect(byName(first.exporter.getFinishedSpans(), "codex.turn")).toHaveLength(1);
    expect(byName(second.exporter.getFinishedSpans(), "codex.turn")).toHaveLength(1);
  } finally {
    a.unpatch();
    b.unpatch();
  }
});

it("retains active-owner duplicate protection and does not let a non-owner unpatch", async () => {
  const first = memoryProvider();
  const second = memoryProvider();
  const mod = fakeModule();
  const thread = new mod.Codex([scripted(toolTurn())]).startThread();
  const a = new CodexSDKInstrumentation({ tracerProvider: first.provider });
  const b = new CodexSDKInstrumentation({ tracerProvider: second.provider });
  try {
    a.wrapThread(thread);
    const active = thread.runStreamedInternal;
    a.wrapThread(thread);
    b.wrapThread(thread);
    b.unpatch();
    expect(thread.runStreamedInternal).toBe(active);
    expect(isPatched(thread)).toBe(true);
    await thread.run("first");
    expect(byName(first.exporter.getFinishedSpans(), "codex.turn")).toHaveLength(1);
    expect(second.exporter.getFinishedSpans()).toHaveLength(0);
  } finally {
    a.unpatch();
    b.unpatch();
  }
});

it("restores the original own property descriptor rather than just its value", () => {
  const mod = fakeModule();
  const thread = new mod.Codex([]).startThread();
  const descriptor = { value: thread.runStreamedInternal, writable: false, enumerable: true, configurable: true };
  Object.defineProperty(thread, "runStreamedInternal", descriptor);
  const instrumentation = new CodexSDKInstrumentation();
  instrumentation.wrapThread(thread);
  instrumentation.unpatch();
  expect(Object.getOwnPropertyDescriptor(thread, "runStreamedInternal")).toEqual(descriptor);
  expect(isPatched(thread)).toBe(false);
});

it("does not overwrite a method replaced by the application after wrapping", () => {
  const mod = fakeModule();
  const thread = new mod.Codex([]).startThread();
  const instrumentation = new CodexSDKInstrumentation();
  instrumentation.wrapThread(thread);
  const replacement = async function* () { yield { type: "turn.completed" }; };
  thread.runStreamedInternal = replacement;
  instrumentation.unpatch();
  expect(thread.runStreamedInternal).toBe(replacement);
  expect(isPatched(thread)).toBe(false);
});

it("restores threads created by wrapCodex and permits a fresh owner", async () => {
  const first = memoryProvider();
  const second = memoryProvider();
  const mod = fakeModule();
  const a = new CodexSDKInstrumentation({ tracerProvider: first.provider });
  const b = new CodexSDKInstrumentation({ tracerProvider: second.provider });
  try {
    const codex = a.wrapCodex(new mod.Codex([scripted(toolTurn()), scripted(toolTurn())]));
    const thread = codex.startThread();
    await thread.run("first");
    a.unpatch();
    b.wrapThread(thread);
    await thread.run("second");
    expect(byName(first.exporter.getFinishedSpans(), "codex.turn")).toHaveLength(1);
    expect(byName(second.exporter.getFinishedSpans(), "codex.turn")).toHaveLength(1);
  } finally {
    a.unpatch();
    b.unpatch();
  }
});
