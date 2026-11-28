// Skills attribution is strictly opt-in (traceAI #791 review). Without
// runtimeAttributes: true, or an explicitly supplied runtime processor, spans,
// contexts, projection and export match dev; with it, privacy is never weaker.
// Each test names the review finding it pins.
import { Attributes, Context, ROOT_CONTEXT } from "@opentelemetry/api";
import {
  BasicTracerProvider, InMemorySpanExporter, ReadableSpan, Sampler, SamplingDecision, SimpleSpanProcessor, Span, SpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { projectAttributes, protectedFamily } from "../attribute-projection";
import { privacyFilter } from "../export-privacy";
import { register } from "../otel";
import { FutureAGIRuntimeAttributesSpanProcessor } from "../trace/runtimeAttributesProcessor";
import { FITracer } from "../trace/trace-config/FITracer";
import { AGENT_VIEW, FILL_IDENTITY } from "../trace/trace-config/agentIdentity";
import { maskAttributes } from "../trace/trace-config/maskingRules";
import { redactPiiInString } from "../trace/trace-config/piiRedaction";
import { generateTraceConfig } from "../trace/trace-config/traceConfig";
import type { TraceConfigOptions } from "../trace/trace-config/types";

const CARRIER = Symbol.for("futureagi.runtime.span_attributes.v1");
const SOURCES = Symbol.for("futureagi.runtime.context-agent.sources.v1");
const globals = globalThis as unknown as Record<symbol, unknown>;
const uuid = "44444444-4444-4444-8444-444444444444";
const env = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...env }; delete globals[SOURCES]; });

const app = (seen: Attributes[] = []): SpanProcessor & { seen: Attributes[] } => ({
  seen,
  onStart: (span: Span) => { seen.push({ ...span.attributes }); },
  onEnd: () => {}, forceFlush: async () => {}, shutdown: async () => {},
});

/** One standalone FITracer span on a plain SDK provider; optionally opted in. */
async function standalone(attributes: Attributes, options: { limit?: number; runtime?: boolean; before?: SpanProcessor[];
  write?: (span: any) => void; traceConfig?: TraceConfigOptions } = {}) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({
    ...(options.limit ? { spanLimits: { attributeCountLimit: options.limit } } : {}),
    spanProcessors: [...(options.runtime ? [new FutureAGIRuntimeAttributesSpanProcessor()] : []), ...(options.before ?? []), new SimpleSpanProcessor(exporter)],
  });
  try {
    const span: any = new FITracer({ tracer: provider.getTracer("opt-in"), traceConfig: options.traceConfig }).startSpan("s", { attributes });
    options.write?.(span);
    span.end();
    const native = exporter.getFinishedSpans()[0] as ReadableSpan;
    return { native: { ...native.attributes }, exported: privacyFilter().span(native).attributes };
  } finally { await provider.shutdown(); }
}

describe("without opt-in, behaviour equals dev", () => {
  // Finding 1: shape alone is not runtime provenance.
  test("an ordinary slug-shaped agent name keeps dev's omission after attribute loss", async () => {
    const { native, exported } = await standalone({ "gen_ai.agent.name": "abcd", "z.extra": "x" }, { limit: 1 });
    expect(native).toEqual({ "gen_ai.agent.name": "abcd" });
    expect(exported["gen_ai.agent.name"]).toBe("<omitted: 4 base64 chars>");
  });

  // Findings 2 and 10: error classification keeps its slot.
  test("an agent name never displaces error.type", async () => {
    const attributes = { "error.type": "Timeout", "gen_ai.agent.name": "router" };
    expect(protectedFamily("gen_ai.agent.name")).toBeUndefined();
    expect(projectAttributes(attributes, 1).attributes).toEqual({ "error.type": "Timeout" });
    expect((await standalone(attributes, { limit: 1 })).native).toEqual({ "error.type": "Timeout" });
  });

  // Finding 4: the sampler and processors see the caller's own Context.
  test("an explicit ROOT_CONTEXT reaches the sampler unchanged", async () => {
    const seen: Context[] = [];
    const sampler: Sampler = {
      shouldSample: ctx => { seen.push(ctx); return { decision: ctx === ROOT_CONTEXT ? SamplingDecision.RECORD_AND_SAMPLED : SamplingDecision.NOT_RECORD }; },
      toString: () => "root-only",
    };
    for (const runtime of [false, true]) {
      const exporter = new InMemorySpanExporter();
      const provider = new BasicTracerProvider({ sampler, spanProcessors: [...(runtime ? [new FutureAGIRuntimeAttributesSpanProcessor()] : []), new SimpleSpanProcessor(exporter)] });
      try {
        new FITracer({ tracer: provider.getTracer("opt-in") }).startSpan("root", {}, ROOT_CONTEXT).end();
        expect(exporter.getFinishedSpans().map(span => span.name)).toEqual(["root"]);
      } finally { await provider.shutdown(); }
    }
    expect(seen.every(ctx => ctx === ROOT_CONTEXT)).toBe(true);
    // Through register(): the FI registry also receives the same object.
    for (const runtimeAttributes of [undefined, true]) {
      const exporter = new InMemorySpanExporter();
      const parents: Context[] = [];
      const observer: SpanProcessor = { onStart: (_span, ctx) => { parents.push(ctx); }, onEnd: () => {}, forceFlush: async () => {}, shutdown: async () => {} };
      const provider = register({ projectName: "opt-in", setGlobalTracerProvider: false, sampler, runtimeAttributes, spanProcessors: [observer, new SimpleSpanProcessor(exporter)] });
      try {
        new FITracer({ tracer: provider.getTracer("opt-in") }).startSpan("root", {}, ROOT_CONTEXT).end();
        expect(exporter.getFinishedSpans().map(span => span.name)).toEqual(["root"]);
        expect(parents).toEqual([ROOT_CONTEXT]);
        expect(parents[0]).toBe(ROOT_CONTEXT);
      } finally { await provider.shutdown(); }
    }
  });

  // Finding 5: the caller's start value wins over an onStart rewrite, as on dev.
  test.each(["gen_ai.agent.name", "gen_ai.agent.id"])("caller-wins %s precedence over a custom onStart", async key => {
    const value = key === "gen_ai.agent.id" ? uuid : "checkout";
    const rewrite: SpanProcessor = {
      onStart: (span: Span) => { span.setAttribute(key, key === "gen_ai.agent.id" ? "55555555-5555-4555-8555-555555555555" : "router"); },
      onEnd: () => {}, forceFlush: async () => {}, shutdown: async () => {},
    };
    expect((await standalone({ [key]: value }, { before: [rewrite] })).native[key]).toBe(value);
  });

  // Finding 6: a retained second field cannot export a dropped session id.
  test("a session id copied into fi.context.id keeps dev's omission", async () => {
    const { native, exported } = await standalone({ "fi.context.id": uuid, "fi.context.session_id": uuid }, { limit: 1 });
    expect(native).toEqual({ "fi.context.id": uuid });
    expect(exported["fi.context.id"]).toBe("<omitted: 36 base64 chars>");
  });

  test("SDK identity calls are no-ops on a span whose provider did not opt in", async () => {
    const { native } = await standalone({}, { write: span => span[FILL_IDENTITY]("gen_ai.agent.name", "planner") });
    expect(native["gen_ai.agent.name"]).toBeUndefined();
  });
});

describe("with opt-in, privacy is never weaker than dev", () => {
  test("runtime keys rank after every dev family and still beat ordinary content", async () => {
    expect(protectedFamily("gen_ai.agent.name", true)).toBeGreaterThan(protectedFamily("traceai.capture.layer")!);
    expect(projectAttributes({ "error.type": "Timeout", "gen_ai.agent.name": "router" }, 1, undefined, true).attributes).toEqual({ "error.type": "Timeout" });
    expect((await standalone({ "error.type": "Timeout", "gen_ai.agent.name": "router" }, { limit: 1, runtime: true })).native).toEqual({ "error.type": "Timeout" });
    // Dev's dropped-content marker (family 0) keeps its slot ahead of runtime keys.
    expect(projectAttributes({ "gen_ai.agent.name": "router", "input.value": "hello" }, 1, undefined, true).attributes)
      .toEqual({ "traceai.capture.dropped_content_count": 1 });
    // With a slot beside the marker, the runtime key wins over ordinary content.
    const two = projectAttributes({ "gen_ai.agent.name": "router", "input.value": "hello", "output.value": "bye" }, 2, undefined, true).attributes;
    expect(two["gen_ai.agent.name"]).toBe("router");
    expect(two).toHaveProperty(["traceai.capture.dropped_content_count"]);
    expect(two).not.toHaveProperty(["input.value"]);
    expect(two).not.toHaveProperty(["output.value"]);
  });

  test("the onStart identity rewrite is the committed write only on an opted-in span", async () => {
    const rewrite: SpanProcessor = {
      onStart: (span: Span) => { span.setAttribute("gen_ai.agent.name", "router"); },
      onEnd: () => {}, forceFlush: async () => {}, shutdown: async () => {},
    };
    expect((await standalone({ "gen_ai.agent.name": "checkout" }, { runtime: true, before: [rewrite] })).native["gen_ai.agent.name"]).toBe("router");
  });

  // Finding 1 (positive half) and finding 6: only SDK-written values carry runtime evidence.
  test("an SDK-written identity survives attribute loss unless it equals a credential-named sibling", async () => {
    const fill = (span: any) => span[FILL_IDENTITY]("gen_ai.agent.id", uuid);
    const kept = await standalone({ "z.extra": "x" }, { limit: 1, runtime: true, write: fill });
    expect(kept.native).toEqual({ "gen_ai.agent.id": uuid });
    expect(kept.exported["gen_ai.agent.id"]).toBe(uuid);
    const shared = await standalone({ "app.session_token": uuid }, { limit: 1, runtime: true, write: fill });
    expect(shared.native).toEqual({ "gen_ai.agent.id": uuid });
    expect(shared.exported["gen_ai.agent.id"]).toBe("<omitted: 36 base64 chars>");
    // A later ordinary write of the same key is not an SDK write.
    const overwritten = await standalone({ "z.extra": "x" }, { limit: 1, runtime: true, write: span => { fill(span); span.setAttribute("gen_ai.agent.id", uuid); } });
    expect(overwritten.exported["gen_ai.agent.id"]).toBe("<omitted: 36 base64 chars>");
  });

  // Finding 7: masked before stamping, so application onStart never sees the raw value.
  test.each([
    ["hideInputs", { hideInputs: true }, "gen_ai.agent.description", "short private plan"],
    ["piiRedaction", { piiRedaction: true }, "gen_ai.agent.name", "jane@example.com"],
  ] as const)("carrier values are masked with the provider policy (%s)", async (_label, traceConfig, key, raw) => {
    const expected = maskAttributes({ config: generateTraceConfig(traceConfig), attributes: { [key]: raw } })[key];
    expect(expected).not.toBe(raw);
    const attrs = { [key]: raw };
    const observer = jest.fn();
    globals[SOURCES] = new WeakMap([[attrs, observer]]);
    const parent = ROOT_CONTEXT.setValue(CARRIER, { v: 1, attrs, reset_skills: false });
    const seen = app();
    const exporter = new InMemorySpanExporter();
    // Through the provider: register() passes its own traceConfig to the processor.
    const provider = register({ projectName: "opt-in", setGlobalTracerProvider: false, runtimeAttributes: true, traceConfig, spanProcessors: [seen, new SimpleSpanProcessor(exporter)] });
    try {
      provider.getTracer("opt-in").startSpan("s", {}, parent).end();
      expect(seen.seen[0][key]).toBe(expected);
      expect(exporter.getFinishedSpans()[0].attributes[key]).toBe(expected);
      expect(observer).not.toHaveBeenCalled();
    } finally { await provider.shutdown(); }
  });

  // Finding 7 (supplied path): a caller-supplied processor masks with the provider's policy too.
  test.each([
    ["hideInputs", { hideInputs: true }, "gen_ai.agent.description", "short private plan"],
    ["piiRedaction", { piiRedaction: true }, "gen_ai.agent.name", "jane@example.com"],
  ] as const)("a supplied runtime processor adopts the provider policy (%s)", async (_label, traceConfig, key, raw) => {
    const expected = maskAttributes({ config: generateTraceConfig(traceConfig), attributes: { [key]: raw } })[key];
    expect(expected).not.toBe(raw);
    const parent = ROOT_CONTEXT.setValue(CARRIER, { v: 1, attrs: { [key]: raw }, reset_skills: false });
    for (const runtimeAttributes of [undefined, true, false]) {
      const seen = app();
      const exporter = new InMemorySpanExporter();
      const provider = register({ projectName: "opt-in", setGlobalTracerProvider: false, runtimeAttributes, traceConfig,
        spanProcessors: [new FutureAGIRuntimeAttributesSpanProcessor(), seen, new SimpleSpanProcessor(exporter)] });
      try {
        provider.getTracer("opt-in").startSpan("s", {}, parent).end();
        expect(seen.seen[0][key]).toBe(expected);
        expect(exporter.getFinishedSpans()[0].attributes[key]).toBe(expected);
      } finally { await provider.shutdown(); }
    }
  });

  test("adopting a provider policy never loosens an explicitly configured processor", async () => {
    const raw = "short private plan";
    const expected = maskAttributes({ config: generateTraceConfig({ hideInputs: true }), attributes: { "gen_ai.agent.description": raw } })["gen_ai.agent.description"];
    const parent = ROOT_CONTEXT.setValue(CARRIER, { v: 1, attrs: { "gen_ai.agent.description": raw }, reset_skills: false });
    const seen = app();
    const provider = register({ projectName: "opt-in", setGlobalTracerProvider: false, traceConfig: { hideInputs: false },
      spanProcessors: [new FutureAGIRuntimeAttributesSpanProcessor({ traceConfig: { hideInputs: true } }), seen] });
    try {
      provider.getTracer("opt-in").startSpan("s", {}, parent).end();
      expect(seen.seen[0]["gen_ai.agent.description"]).toBe(expected);
    } finally { await provider.shutdown(); }
  });

  test("an unmasked carrier name still reaches the SDK observer", async () => {
    const attrs = { "gen_ai.agent.name": "planner" };
    const observer = jest.fn();
    globals[SOURCES] = new WeakMap([[attrs, observer]]);
    const provider = new BasicTracerProvider({ spanProcessors: [new FutureAGIRuntimeAttributesSpanProcessor()] });
    try {
      provider.getTracer("opt-in").startSpan("s", {}, ROOT_CONTEXT.setValue(CARRIER, { v: 1, attrs })).end();
      expect(observer).toHaveBeenCalledTimes(1);
    } finally { await provider.shutdown(); }
  });

  test("a complete stamped map exports unchanged", async () => {
    const attrs = { "fi.context.name": "support", "gen_ai.agent.name": "refund-agent", "fi.skills.version_ids": [uuid], "fi.skills.count": 1 };
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new FutureAGIRuntimeAttributesSpanProcessor(), new SimpleSpanProcessor(exporter)] });
    try {
      provider.getTracer("opt-in").startSpan("s", {}, ROOT_CONTEXT.setValue(CARRIER, { v: 1, attrs })).end();
      expect(privacyFilter().span(exporter.getFinishedSpans()[0]).attributes).toEqual(attrs);
    } finally { await provider.shutdown(); }
  });
});

// Finding 9: read-back never returns more than the strictest exporter would.
test("identity read-back applies PII redaction even when the wrapper policy is off", async () => {
  const email = "jane@example.com";
  const provider = new BasicTracerProvider();
  try {
    const span: any = new FITracer({ tracer: provider.getTracer("opt-in"), traceConfig: { piiRedaction: false } })
      .startSpan("s", { attributes: { "gen_ai.agent.name": email } });
    const view = span[AGENT_VIEW]();
    expect(view.name).toBe(redactPiiInString(email));
    expect(view.name).not.toContain("@");
    span.end();
  } finally { await provider.shutdown(); }
});

// Finding 12: work per span start is bounded by the caps, not by the snapshot.
describe("bounded consumption", () => {
  async function start(attrs: object, spans = 1) {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new FutureAGIRuntimeAttributesSpanProcessor(), new SimpleSpanProcessor(exporter)] });
    try {
      const parent = ROOT_CONTEXT.setValue(CARRIER, Object.freeze({ v: 1, attrs }));
      for (let i = 0; i < spans; i++) provider.getTracer("opt-in").startSpan("s", {}, parent).end();
      return exporter.getFinishedSpans().map(span => ({ ...span.attributes }));
    } finally { await provider.shutdown(); }
  }

  test("a huge snapshot reads at most the scan cap of keys and the list cap of items", async () => {
    let reads = 0;
    const attrs: Record<string, unknown> = {};
    for (let i = 0; i < 10_000; i++) Object.defineProperty(attrs, `fi.context.k${i}`, { enumerable: true, get: () => { reads++; return i; } });
    const [result] = await start(attrs);
    expect(reads).toBeLessThanOrEqual(256);
    expect(Object.keys(result)).toHaveLength(62);
    let items = 0;
    const ids = new Proxy(Array.from({ length: 100_000 }, () => uuid), {
      get: (target, key, receiver) => { if (typeof key === "string" && /^\d+$/.test(key)) items++; return Reflect.get(target, key, receiver); },
    });
    const [capped] = await start({ "fi.skills.version_ids": ids });
    expect(items).toBeLessThanOrEqual(32);
    expect(capped).toEqual({ "fi.skills.version_ids": Array(32).fill(uuid), "fi.skills.count": 32, "fi.skills.truncated": true });
  });

  test("a frozen snapshot is filtered once across spans; a mutable one each time", async () => {
    let reads = 0;
    const frozen = Object.freeze(Object.defineProperty({}, "fi.context.name", { enumerable: true, get: () => { reads++; return "support"; } }));
    const results = await start(frozen, 3);
    expect(results.map(attrs => attrs["fi.context.name"])).toEqual(["support", "support", "support"]);
    expect(reads).toBe(1);
    reads = 0;
    const mutable = Object.defineProperty({}, "fi.context.name", { enumerable: true, get: () => { reads++; return "support"; } });
    await start(mutable, 3);
    expect(reads).toBe(3);
    // A frozen map whose list is mutable is not cached either.
    const list = [uuid];
    const mixed = Object.freeze({ "fi.skills.version_ids": list });
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new FutureAGIRuntimeAttributesSpanProcessor(), new SimpleSpanProcessor(exporter)] });
    try {
      const parent = ROOT_CONTEXT.setValue(CARRIER, { v: 1, attrs: mixed });
      provider.getTracer("opt-in").startSpan("a", {}, parent).end();
      list.push("55555555-5555-4555-8555-555555555555");
      provider.getTracer("opt-in").startSpan("b", {}, parent).end();
      expect(exporter.getFinishedSpans().map(span => span.attributes["fi.skills.count"])).toEqual([1, 2]);
    } finally { await provider.shutdown(); }
  });
});
