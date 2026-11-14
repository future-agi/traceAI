import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { ROOT_CONTEXT, trace } from "@opentelemetry/api";
import { InstrumentationBase, type InstrumentationConfig } from "@opentelemetry/instrumentation";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer, runInFIContext } from "@traceai/fi-core";
import { activeContext, applicationContextManager, contextBoundProvider, runIntegrationCall } from "../src/agent-context";
import type { Config } from "../src/config";
import { enableLibraries, type IntegrationStatus } from "../src/libraries";

jest.mock("node:module", () => {
  const actual = jest.requireActual<typeof import("node:module")>("node:module");
  return { ...actual, createRequire: jest.fn(actual.createRequire) };
});
jest.mock("../src/integrations.json", () => [{
  name: "fixture", package: "integration-fixture", targets: [], modules: [], className: "Instrumentation",
}]);

// An application with no OpenTelemetry context manager. An integration the agent loads
// starts its model span with FITracer.startSpan on an agent-bound tracer, and the agent
// runs each patched SDK call in its own integration call scope. These tests pin which
// span the agent treats as active, inside and outside such a call.
const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
// The tracer an integration gets from the agent: an FI tracer over an agent-bound tracer.
const integration = new FITracer({ tracer: contextBoundProvider(provider).getTracer("integration") });
// A tracer that is not the agent's, for a context the application sets explicitly.
const other = provider.getTracer("other");
const tick = () => new Promise<void>(done => setImmediate(done));
const activeSpanId = () => trace.getSpan(activeContext())?.spanContext().spanId;
function finished(name: string) {
  const span = exporter.getFinishedSpans().find(item => item.name === name);
  if (!span) throw new Error(`span ${name} was not ended`);
  return span;
}
const idOf = (name: string) => finished(name).spanContext().spanId;
const parentOf = (name: string) => finished(name).parentSpanContext?.spanId;

beforeEach(() => exporter.reset());
afterAll(() => provider.shutdown());

test("no context manager is registered in this file", () => {
  expect(applicationContextManager()).toBe(false);
});

test("inside an integration call, the span the integration starts is active for the rest of that call, and spans started in it nest", async () => {
  let inside: string | undefined, afterAwait: string | undefined;
  await runIntegrationCall(async () => {
    const model = integration.startSpan("model");
    inside = activeSpanId();
    integration.startSpan("child").end();
    await tick();
    afterAwait = activeSpanId();
    integration.startSpan("child-after-await").end();
    model.end();
  });
  const model = idOf("model");
  expect(parentOf("model")).toBeUndefined();
  expect({ inside, afterAwait }).toEqual({ inside: model, afterAwait: model });
  expect(parentOf("child")).toBe(model);
  expect(parentOf("child-after-await")).toBe(model);
});

test("a patched call made inside an integration call parents under that call's span", async () => {
  await runIntegrationCall(async () => {
    const outer = integration.startSpan("outer-model");
    await runIntegrationCall(async () => {
      const inner = integration.startSpan("inner-model");
      await tick();
      integration.startSpan("in-inner").end();
      inner.end();
    });
    integration.startSpan("after-inner").end();
    outer.end();
  });
  expect(parentOf("inner-model")).toBe(idOf("outer-model"));
  expect(parentOf("in-inner")).toBe(idOf("inner-model"));
  // Once the inner call has returned, its span is not active in the outer call.
  expect(parentOf("after-inner")).toBe(idOf("outer-model"));
});

test("after the call returns, a span still open is not active for the caller, and the caller's next span has no parent from it", async () => {
  const open: { end(): void }[] = [];
  await runIntegrationCall(async () => { open.push(integration.startSpan("model")); await tick(); });
  const after = activeSpanId();
  integration.startSpan("caller-after").end();
  await tick();
  const later = activeSpanId();
  for (const span of open) span.end();
  expect({ after, later }).toEqual({ after: undefined, later: undefined });
  expect(parentOf("caller-after")).toBeUndefined();
});

test("a sibling flow started in the same tick as the call does not see the call's span", async () => {
  const open: { end(): void }[] = [];
  const call = runIntegrationCall(async () => { open.push(integration.startSpan("model")); await tick(); await tick(); });
  const beside = activeSpanId();
  integration.startSpan("beside").end();
  const sibling = (async () => { await tick(); return activeSpanId(); })();
  const [, siblingActive] = await Promise.all([call, sibling]);
  for (const span of open) span.end();
  expect({ beside, siblingActive }).toEqual({ beside: undefined, siblingActive: undefined });
  expect(parentOf("beside")).toBeUndefined();
});

test("inside the call, a context set later with runInFIContext or startActiveSpan takes precedence over the call's span (innermost wins)", async () => {
  let detached: string | undefined, explicitSpan: string | undefined, afterInner: string | undefined;
  await runIntegrationCall(async () => {
    const model = integration.startSpan("model");
    // A context the application sets explicitly, away from the call's span.
    const explicit = other.startSpan("explicit", {}, ROOT_CONTEXT);
    explicitSpan = explicit.spanContext().spanId;
    await runInFIContext(trace.setSpan(ROOT_CONTEXT, explicit), async () => {
      detached = activeSpanId();
      await tick();
      integration.startSpan("in-explicit").end();
    });
    // A context made from the active one, with a span of its own.
    const wrapper = other.startSpan("wrapper", {}, activeContext());
    await runInFIContext(trace.setSpan(activeContext(), wrapper), async () => {
      await tick();
      integration.startSpan("in-wrapper").end();
    });
    wrapper.end();
    // An active span started inside the call.
    await integration.startActiveSpan("active", async span => {
      await tick();
      integration.startSpan("in-active").end();
      span.end();
    });
    afterInner = activeSpanId();
    explicit.end();
    model.end();
  });
  const model = idOf("model");
  expect(detached).toBe(explicitSpan);
  expect(parentOf("in-explicit")).toBe(explicitSpan);
  expect(parentOf("wrapper")).toBe(model);
  expect(parentOf("in-wrapper")).toBe(idOf("wrapper"));
  expect(parentOf("active")).toBe(model);
  expect(parentOf("in-active")).toBe(idOf("active"));
  // After those inner contexts, the call's own span is active again.
  expect(afterInner).toBe(model);
});

test("inside the call, a span that has ended is no longer active", async () => {
  let ended: string | undefined;
  await runIntegrationCall(async () => {
    const model = integration.startSpan("model");
    model.end();
    ended = activeSpanId();
    await tick();
    integration.startSpan("after-end").end();
  });
  expect(ended).toBeUndefined();
  expect(parentOf("after-end")).toBeUndefined();
});

test("the launcher keeps no context store of its own and never enters a context for the caller's flow", () => {
  // Source text: the private context lives in fi-core's store, written with run semantics only.
  const source = readFileSync(resolve(__dirname, "../src/agent-context.ts"), "utf8");
  expect(source).not.toContain("enterWith");
  expect(source).not.toContain("AsyncLocalStorage");
});

describe("the launcher's per-instance integration call hook", () => {
  const key = Symbol.for("traceai.auto.integration-call.v1");
  class FixtureInstrumentation extends InstrumentationBase {
    constructor(config: InstrumentationConfig) { super("integration-fixture", "1.0.0", config); }
    protected init() { return []; }
  }
  let instance: FixtureInstrumentation;
  let disable: () => void = () => {};
  function hook(): <T>(fn: () => T) => T {
    const scope = (instance as unknown as Record<symbol, unknown>)[key];
    expect(typeof scope).toBe("function");
    return scope as <T>(fn: () => T) => T;
  }
  beforeEach(() => {
    const Instrumentor = jest.fn(({ instrumentationConfig }: { instrumentationConfig: InstrumentationConfig }) => {
      const result = new FixtureInstrumentation(instrumentationConfig);
      jest.spyOn(result, "enable");
      return result;
    });
    // No SDK modules or loader are needed: use the real registration lifecycle
    // with an instrumentor whose patching does not go through the launcher's _wrap.
    const load = Object.assign(() => ({ Instrumentation: Instrumentor }), { resolve: () => "integration-fixture" });
    jest.mocked(createRequire).mockReturnValue(load as unknown as ReturnType<typeof createRequire>);
    const config: Config = {
      forwardApplicationSpans: false, propagateOutgoingAdapters: false, propagateOutgoing: "none",
      baseUrl: "http://localhost", projectName: "integration-fixture", projectType: "observe",
      instrumentations: ["fixture"], disabledInstrumentations: [], wireCapture: true, wireMaxBodyBytes: 1024,
      sampleRate: 1, debug: false, configFile: "", sdkDisabled: false, traceConfig: {}, mastraSpanProducer: "exporter",
    };
    const statuses: IntegrationStatus[] = [{ name: "fixture", status: "pending" }];
    const warn = jest.fn();
    disable = enableLibraries(config, {}, contextBoundProvider(provider), statuses, warn);
    expect(warn).not.toHaveBeenCalled();
    expect(Instrumentor).toHaveBeenCalledTimes(1);
    instance = Instrumentor.mock.results[0].value;
    expect(instance.enable).toHaveBeenCalledTimes(1);
  });
  afterEach(() => {
    disable();
    jest.restoreAllMocks();
    jest.mocked(createRequire).mockImplementation(jest.requireActual<typeof import("node:module")>("node:module").createRequire);
  });

  test("an enabled instrumentor has a non-enumerable, configurable call hook only on that instance", () => {
    expect(Object.getOwnPropertyDescriptor(instance, key)).toMatchObject({
      value: hook(), enumerable: false, configurable: true,
    });
    const standalone = new FixtureInstrumentation({ enabled: false });
    expect((standalone as unknown as Record<symbol, unknown>)[key]).toBeUndefined();
    expect(Object.getOwnPropertyDescriptor(globalThis, key)).toBeUndefined();
  });

  test("without an application context manager, the hook parents work inside the call but not work after it returns", async () => {
    expect(applicationContextManager()).toBe(false);
    const scope = hook(), open: { end(): void }[] = [];
    let inside: string | undefined, afterAwait: string | undefined;
    const call = scope(async () => {
      open.push(integration.startSpan("hook-model"));
      inside = activeSpanId();
      integration.startSpan("hook-child").end();
      await tick();
      afterAwait = activeSpanId();
      integration.startSpan("hook-child-after-await").end();
    });
    const afterReturn = activeSpanId();
    integration.startSpan("hook-beside").end();
    await call;
    const afterCall = activeSpanId();
    integration.startSpan("hook-after").end();
    // Keep the model span open until after the caller has started its next work.
    for (const span of open) span.end();
    const model = idOf("hook-model");
    expect(parentOf("hook-model")).toBeUndefined();
    expect({ inside, afterAwait }).toEqual({ inside: model, afterAwait: model });
    expect(parentOf("hook-child")).toBe(model);
    expect(parentOf("hook-child-after-await")).toBe(model);
    expect({ afterReturn, afterCall }).toEqual({ afterReturn: undefined, afterCall: undefined });
    expect(parentOf("hook-beside")).toBeUndefined();
    expect(parentOf("hook-after")).toBeUndefined();
    expect(applicationContextManager()).toBe(false);
  });

  test("a retained hook runs the callback without a call scope after disable", () => {
    const scope = hook(), open: { end(): void }[] = [];
    const value = {};
    disable();
    expect(hook()).toBe(scope);
    let inside: string | undefined;
    const fn = jest.fn(() => {
      open.push(integration.startSpan("disabled-model"));
      inside = activeSpanId();
      integration.startSpan("disabled-child").end();
      return value;
    });
    expect(scope(fn)).toBe(value);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(inside).toBeUndefined();
    expect(activeSpanId()).toBeUndefined();
    for (const span of open) span.end();
    expect(parentOf("disabled-child")).toBeUndefined();
  });

  test.each(["enabled", "disabled"])("the %s hook preserves values, promise and stream identity, and synchronous throws", state => {
    const scope = hook();
    if (state === "disabled") disable();
    const value = {}, promise = Object.assign(Promise.resolve(value), { withResponse: jest.fn() });
    const stream = { [Symbol.asyncIterator]: jest.fn() };
    for (const result of [value, promise, stream, undefined, 42]) {
      const fn = jest.fn(() => result);
      expect(scope(fn)).toBe(result);
      expect(fn).toHaveBeenCalledTimes(1);
    }
    expect(promise.withResponse).not.toHaveBeenCalled();
    expect(stream[Symbol.asyncIterator]).not.toHaveBeenCalled();
    const error = new Error("callback failed"), fn = jest.fn(() => { throw error; });
    let caught: unknown;
    try { scope(fn); } catch (cause) { caught = cause; }
    expect(caught).toBe(error);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(activeSpanId()).toBeUndefined();
  });
});

// Last in this file: on code that still enters the span for the caller's flow, this
// case leaves it entered in the test's own flow.
test("outside any integration call, startSpan makes nothing active: the caller's next span and request do not run under it", async () => {
  const unscoped = integration.startSpan("unscoped");
  const now = activeSpanId();
  integration.startSpan("next").end();
  await tick();
  const later = activeSpanId();
  unscoped.end();
  expect({ now, later }).toEqual({ now: undefined, later: undefined });
  expect(parentOf("next")).toBeUndefined();
});
