import { context, trace, SpanStatusCode } from "@opentelemetry/api";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { GuardrailsInstrumentation, isPatched, _resetPatchedStateForTesting } from "../instrumentation";
import { VERSION } from "../version";

const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
provider.register();
const instances: GuardrailsInstrumentation[] = [];

function instrument(config: ConstructorParameters<typeof GuardrailsInstrumentation>[0] = {}) {
  const inst = new GuardrailsInstrumentation(config);
  instances.push(inst);
  inst.setTracerProvider(provider);
  return inst;
}

function guardReturning(result: unknown) {
  return class Guard {
    async parse(_input: string, _options?: object) { return result; }
  };
}

beforeEach(() => { exporter.reset(); });
afterEach(() => {
  for (const inst of instances.splice(0)) inst.disable();
  _resetPatchedStateForTesting();
});
afterAll(async () => { await provider.shutdown(); });

test("runtime version matches package metadata", () => {
  expect(VERSION).toBe(require("../../package.json").version);
});

test.each([
  [{ validationPassed: false }, false],
  [{ validationPassed: true }, true],
  [{ validationPassed: false, valid: true, validated: true }, false],
  [{ validationPassed: true, valid: false, validated: false }, true],
  [{ validated: false }, false],
  [{ valid: false }, false],
  [{ valid: true, validated: false }, false],
  [{ validated: true }, true],
  [{ valid: true }, true],
  [{}, undefined],
  [undefined, undefined],
  [null, undefined],
  [{ validationPassed: "false" }, undefined],
  [{ validated: "yes" }, undefined],
])("records the actual boolean outcome for %j", async (result, expected) => {
  const Guard = guardReturning(result);
  instrument().manuallyInstrument({ Guard });
  expect(await new Guard().parse("candidate")).toBe(result);
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  expect(spans[0].attributes["guardrails.validation_passed"]).toBe(expected);
  expect(spans[0].attributes["fi.span.kind"]).toBe("GUARDRAIL");
  expect(spans[0].status.code).toBe(SpanStatusCode.OK);
});

test.each(["validate", "parse", "call"])("keeps the %s entry point", async (method) => {
  const original = jest.fn().mockResolvedValue({ validationPassed: false });
  const Guard = class {};
  Object.assign(Guard.prototype, { [method]: original });
  instrument().manuallyInstrument({ Guard });
  const instance: any = new Guard();
  const options = { numReasks: 0 };
  await instance[method]("candidate", options);
  expect(original).toHaveBeenCalledWith("candidate", options);
  expect(original.mock.instances[0]).toBe(instance);
  expect(exporter.getFinishedSpans()[0].attributes["guardrails.operation"]).toBe(method);
});

test("accepts an immutable module namespace and does not duplicate spans", async () => {
  const Guard = guardReturning({ validationPassed: true });
  const module = Object.freeze({ Guard });
  const inst = instrument();
  inst.manuallyInstrument(module);
  inst.manuallyInstrument(module);
  instrument().manuallyInstrument({ Guard });
  await new Guard().parse("candidate");
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

test("does not let one SDK copy block another", async () => {
  const Guard1 = guardReturning({ validationPassed: true });
  const Guard2 = guardReturning({ validationPassed: false });
  const inst = instrument();
  inst.manuallyInstrument({ Guard: Guard1 });
  inst.manuallyInstrument({ Guard: Guard2 });
  await new Guard1().parse("candidate");
  await new Guard2().parse("candidate");
  expect(exporter.getFinishedSpans()).toHaveLength(2);
});

test("disable restores manual patches and enable reapplies them", async () => {
  const Guard = guardReturning({ validationPassed: true });
  const original = Guard.prototype.parse;
  const inst = instrument();
  inst.manuallyInstrument({ Guard });
  expect(isPatched()).toBe(true);
  inst.disable();
  expect(Guard.prototype.parse).toBe(original);
  expect(isPatched()).toBe(false);
  await new Guard().parse("not traced");
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  inst.enable();
  await new Guard().parse("traced");
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

test("disabled configuration leaves methods alone until enabled", async () => {
  const Guard = guardReturning({ validationPassed: true });
  const inst = instrument({ instrumentationConfig: { enabled: false } });
  inst.manuallyInstrument({ Guard });
  await new Guard().parse("not traced");
  expect(exporter.getFinishedSpans()).toHaveLength(0);
  inst.enable();
  await new Guard().parse("traced");
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

test("disabling a non-owner does not unpatch the owner", async () => {
  const Guard = guardReturning({ validationPassed: true });
  instrument().manuallyInstrument({ Guard });
  const other = instrument();
  other.manuallyInstrument({ Guard });
  other.disable();
  await new Guard().parse("candidate");
  expect(exporter.getFinishedSpans()).toHaveLength(1);
});

test("ignores absent methods without marking anything patched", () => {
  const inst = instrument();
  inst.manuallyInstrument(null);
  inst.manuallyInstrument({ Guard: {} });
  expect(isPatched()).toBe(false);
});

test("uses constructor privacy configuration without a second enable call", async () => {
  const secret = "synthetic-private-candidate";
  const Guard = guardReturning({ validationPassed: false, rawLlmOutput: secret });
  instrument({ traceConfig: { hideInputs: true, hideOutputs: true } }).manuallyInstrument({ Guard });
  await new Guard().parse(secret);
  const span = exporter.getFinishedSpans()[0];
  expect(JSON.stringify(span.attributes)).not.toContain(secret);
  expect(span.attributes["guardrails.validation_passed"]).toBe(false);
});

test("keeps parentage, active child context, and the original error", async () => {
  const error = new Error("synthetic failure");
  class Guard {
    async parse() {
      await Promise.resolve();
      expect(trace.getSpan(context.active())).toBeDefined();
      provider.getTracer("test").startSpan("child").end();
      throw error;
    }
  }
  instrument().manuallyInstrument({ Guard });
  const parent = provider.getTracer("test").startSpan("parent");
  await context.with(trace.setSpan(context.active(), parent), async () => {
    await expect(new Guard().parse()).rejects.toBe(error);
  });
  parent.end();
  const spans = exporter.getFinishedSpans();
  const guard = spans.find(s => s.name.startsWith("Guardrails"))!;
  expect(guard.parentSpanId).toBe(parent.spanContext().spanId);
  expect(spans.find(s => s.name === "child")!.parentSpanId).toBe(guard.spanContext().spanId);
  expect(guard.status.code).toBe(SpanStatusCode.ERROR);
  expect(guard.attributes["guardrails.validation_passed"]).toBe(false);
});
