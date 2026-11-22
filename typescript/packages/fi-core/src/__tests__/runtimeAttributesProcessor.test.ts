import { Attributes, ROOT_CONTEXT } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor, Span } from "@opentelemetry/sdk-trace-base";
import { FutureAGIRuntimeAttributesSpanProcessor } from "../trace/runtimeAttributesProcessor";
import vectors from "./vectors/processor_v1.json";

const CARRIER = Symbol.for("futureagi.runtime.span_attributes.v1");
const REGISTRY = Symbol.for("futureagi.runtime.skills.v1");
const CONFIG = Symbol.for("futureagi.runtime.skills.config.v1");
const MARKER = Symbol.for("futureagi.runtime.skills.processor.v1");
const globals = globalThis as unknown as Record<symbol, unknown>;
const savedEnv = { ...process.env };
let saved: unknown[];
beforeEach(() => {
  saved = [REGISTRY, CONFIG, MARKER].map(key => globals[key]);
  for (const key of [REGISTRY, CONFIG, MARKER]) delete globals[key];
  delete process.env.FI_SKILLS_TRACING;
});
afterEach(() => {
  [REGISTRY, CONFIG, MARKER].forEach((key, i) => {
    if (saved[i] === undefined) delete globals[key]; else globals[key] = saved[i];
  });
  process.env = { ...savedEnv };
});

async function stamped(carrier: unknown, explicit: Attributes = {}) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new FutureAGIRuntimeAttributesSpanProcessor(), new SimpleSpanProcessor(exporter)] });
  try {
    const parent = carrier == null ? ROOT_CONTEXT : ROOT_CONTEXT.setValue(CARRIER, carrier);
    const span = provider.getTracer("runtime-test").startSpan("unchanged-name", { attributes: explicit }, parent);
    span.end();
    const result = exporter.getFinishedSpans()[0];
    expect(result.name).toBe("unchanged-name");
    return { ...result.attributes };
  } finally { await provider.shutdown(); }
}

test.each(vectors.vectors)("v1: $name", async vector => {
  const get = jest.fn(() => vector.registry);
  globals[REGISTRY] = { v: 1, get };
  expect(await stamped(vector.carrier, vector.explicit)).toEqual(vector.expected);
  expect(get).toHaveBeenCalledTimes(vector.carrier === null ? 1 : 0);
});

test("bounds added attributes, UTF-8 strings and array copies", async () => {
  const ids = Array.from({ length: 40 }, (_, i) => `version-${i}`);
  const attrs: Attributes = { "fi.context.linked_skill_version_ids": ids, "fi.skills.version_ids": ids, "fi.context.name": "é".repeat(129), "fi.skills.count": NaN };
  for (let i = 0; i < 80; i++) attrs[`fi.context.custom_${i}`] = i;
  const result = await stamped({ v: 1, attrs, reset_skills: false });
  // As in Python: 62 copied keys, then the derived count and truncation flag.
  expect(Object.keys(result)).toHaveLength(64);
  expect(result["fi.context.name"]).toBeUndefined();
  expect(result["fi.context.linked_skill_version_ids"]).toBeUndefined();
  expect(result["fi.skills.count"]).toBe(32);
  expect(result["fi.skills.truncated"]).toBe(true);
  expect(result["fi.skills.version_ids"]).toEqual(ids.slice(0, 32));
  expect(result["fi.skills.version_ids"]).not.toBe(ids);
  expect(ids).toHaveLength(40);
});

// Finding 12 / parity: keys past the scan cap are not examined (Python mirrors it).
test("keys past the 256-key scan cap are ignored", async () => {
  const attrs: Record<string, unknown> = {};
  for (let i = 0; i < 300; i++) attrs[`x.k${i}`] = i;
  attrs["fi.context.name"] = "late";
  expect(await stamped({ v: 1, attrs, reset_skills: false })).toEqual({});
  const early: Record<string, unknown> = { "fi.context.name": "early" };
  for (let i = 0; i < 300; i++) early[`x.k${i}`] = i;
  expect(await stamped({ v: 1, attrs: early, reset_skills: false })).toEqual({ "fi.context.name": "early" });
});

test.each(["off", "load_only"])("%s performs no registry work", async mode => {
  process.env.FI_SKILLS_TRACING = mode;
  const get = jest.fn(() => ({ "fi.skills.count": 1 }));
  globals[REGISTRY] = { v: 1, get };
  expect(await stamped(null)).toEqual({});
  expect(get).not.toHaveBeenCalled();
  expect(globals[MARKER]).toBeUndefined();
});

test("optional SDK configuration reader wins over environment and changes dynamically", async () => {
  let mode = "on";
  process.env.FI_SKILLS_TRACING = "off";
  globals[CONFIG] = { v: 1, mode: () => mode };
  const carrier = { v: 1, attrs: { "fi.skills.count": 1 } };
  expect(await stamped(carrier)).toEqual({ "fi.skills.count": 1 });
  process.env.FI_SKILLS_TRACING = "on";
  for (mode of ["off", "load_only", "unknown"]) expect(await stamped(carrier)).toEqual({});
});

test("foreign registry errors cannot affect tracing", async () => {
  globals[REGISTRY] = { v: 1, get: () => { throw new Error("private data"); } };
  expect(await stamped(null)).toEqual({});
});

test("registry lookup receives this span's trace id, not another trace", async () => {
  const get = jest.fn(() => ({ "fi.skills.count": 1 }));
  globals[REGISTRY] = { v: 1, get };
  await stamped(null);
  await stamped(null);
  expect(get).toHaveBeenCalledTimes(2);
  expect(get.mock.calls[0]).not.toEqual(get.mock.calls[1]);
});

test("idempotent stamping preserves explicit false and empty values", () => {
  const processor = new FutureAGIRuntimeAttributesSpanProcessor();
  const attrs: Attributes = { "fi.skills.truncated": false, "gen_ai.agent.name": "" };
  const span = { attributes: attrs, spanContext: () => ({ traceId: "t" }), setAttribute: jest.fn((key: string, value: any) => { attrs[key] = value; }) } as unknown as Span;
  const ctx = ROOT_CONTEXT.setValue(CARRIER, { v: 1, attrs: { "fi.skills.truncated": true, "gen_ai.agent.name": "parent", "fi.context.name": "context" } });
  processor.onStart(span, ctx);
  processor.onStart(span, ctx);
  expect(span.setAttribute).toHaveBeenCalledTimes(1);
  expect(attrs).toEqual({ "fi.skills.truncated": false, "gen_ai.agent.name": "", "fi.context.name": "context" });
});
