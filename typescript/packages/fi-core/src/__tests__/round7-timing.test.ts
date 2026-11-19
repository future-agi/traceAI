import type { Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { DROPPED_CONTENT_COUNT, projectAttributes, protectedFamily } from "../attribute-projection";
import { privacyFilter } from "../export-privacy";
import { isCredentialName } from "../privacy-runtime";
import { registerSecret } from "../secrets";
import { FITracer } from "../trace/trace-config/FITracer";

// Spec R7's exact protected family, not a wildcard that exempts arbitrary names.
const timing = {
  "gen_ai.response.latency": 2,
  "gen_ai.response.time_to_first_chunk": 0,
  "gen_ai.response.time_to_first_text": 0.125,
  "gen_ai.response.time_to_first_token": 0.25,
  "gen_ai.server.time_to_first_token": 0.5,
  "gen_ai.server.time_per_output_token": 0.0625,
  "gen_ai.server.request.duration": 1.75,
};
const identity = {
  "gen_ai.span.kind": "LLM", "gen_ai.response.id": "response-timing-fixture",
  "error.type": "500", "gen_ai.operation.name": "chat",
  "gen_ai.usage.input_tokens": 7, "gen_ai.usage.output_tokens": 3,
  "traceai.capture.layer": "wire",
};
const content: Attributes = {
  "output.value": "ordinary answer", "output.mime_type": "text/plain",
  ...Object.fromEntries(Array.from({ length: 150 }, (_, i) => [
    [`gen_ai.input.messages.${i}.role`, "user"],
    [`gen_ai.input.messages.${i}.content`, `ordinary words ${i}`],
  ]).flat()),
};
const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; });

test.each([16, 32, 128])("R7 numeric timing precedes crowded content at limit %i without changing MIME groups or drop counts", limit => {
  const attributes = { ...content, ...identity, ...timing, [DROPPED_CONTENT_COUNT]: 3 };
  const before = JSON.stringify(attributes);
  const clean = privacyFilter().attributes(attributes);
  const projected = projectAttributes(clean, limit);
  expect(projected.attributes).toMatchObject({ ...identity, ...timing });
  for (const key of Object.keys(timing)) expect(typeof projected.attributes[key]).toBe("number");
  expect(Object.keys(projected.attributes).length).toBeLessThanOrEqual(limit);
  const emittedContent = Object.keys(content).filter(key => key in projected.attributes).length;
  expect(projected.attributes[DROPPED_CONTENT_COUNT]).toBe(3 + Object.keys(content).length - emittedContent);
  expect(projected.dropped).toBe(Object.keys(attributes).length - Object.keys(projected.attributes).length);
  expect("output.value" in projected.attributes).toBe("output.mime_type" in projected.attributes);
  if (limit === 16) expect(emittedContent).toBe(0); // One spare slot cannot split output's MIME pair.
  else expect(projected.attributes["output.value"]).toBe("ordinary answer");
  for (let i = 0; i < 150; i++) {
    expect(`gen_ai.input.messages.${i}.role` in projected.attributes).toBe(`gen_ai.input.messages.${i}.content` in projected.attributes);
  }
  expect(projectAttributes(Object.fromEntries(Object.entries(clean).reverse()), limit)).toEqual(projected);
  expect(JSON.stringify(attributes)).toBe(before);
});

test.each([0, 7, 8, 13, 14, 15])("R7 overflow at %i keeps usage then lexical TIMING then provenance, with no content", limit => {
  const attributes = { ...content, ...identity, ...timing };
  const order = [DROPPED_CONTENT_COUNT, "gen_ai.response.id", "gen_ai.span.kind", "error.type", "gen_ai.operation.name",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", ...Object.keys(timing).sort(), "traceai.capture.layer"];
  const selected = projectAttributes(privacyFilter().attributes(attributes), limit).attributes;
  expect(Object.keys(selected)).toEqual(order.slice(0, limit));
  expect(Object.keys(content).filter(key => key in selected)).toEqual([]);
  if (limit > 0) expect(selected[DROPPED_CONTENT_COUNT]).toBe(Object.keys(content).length);
  for (const key of Object.keys(timing).filter(key => key in selected)) expect(selected[key]).toBe((timing as Attributes)[key]);
});

test("R7 timing is exact, optional, and survives the bounded-candidate fail-closed branch", () => {
  for (const key of Object.keys(timing)) expect(protectedFamily(key)).toBe(5);
  expect(protectedFamily("traceai.capture.layer")).toBe(6);
  expect(protectedFamily("gen_ai.response.custom_latency")).toBeUndefined();
  expect(protectedFamily("gen_ai.response.time_to_first_token.api_key")).toBeUndefined();
  expect(projectAttributes({ "gen_ai.span.kind": "LLM" }, 128).attributes).toEqual({ "gen_ai.span.kind": "LLM" });
  const overBudget = Object.fromEntries(Array.from({ length: 4100 }, (_, i) => [`input.part.${i}`, "ordinary words"]));
  expect(projectAttributes({ ...overBudget, ...timing }, 8)).toEqual({ attributes: { ...timing, [DROPPED_CONTENT_COUNT]: 4100 }, dropped: 4100 });
});

test.each([16, 32, 128])("R7 FITracer reserves numeric timing before SDK admission and after late writes at limit %i", async limit => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: limit }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const tracer = new FITracer({ tracer: provider.getTracer("timing-library") });
    tracer.startSpan("initial", { attributes: { ...content, ...identity, ...timing } }).end();
    const late = tracer.startSpan("late", { attributes: { ...content, ...identity } });
    for (const [key, value] of Object.entries(timing)) late.setAttribute(key, value);
    late.end();
    expect(exporter.getFinishedSpans()).toHaveLength(2);
    for (const span of exporter.getFinishedSpans()) {
      expect(span.attributes).toMatchObject({ ...identity, ...timing });
      expect(Object.keys(span.attributes).length).toBeLessThanOrEqual(limit);
      const emitted = Object.keys(content).filter(key => key in span.attributes).length;
      expect(span.attributes[DROPPED_CONTENT_COUNT]).toBe(Object.keys(content).length - emitted);
      expect(span.droppedAttributesCount).toBeGreaterThan(0);
      expect(privacyFilter().span(span).attributes).toMatchObject(timing);
    }
  } finally { await provider.shutdown(); }
});

test("R7 timing-name exclusions retain numeric values on every detached S1 surface, without exempting credential guards", () => {
  const metrics = { ...timing, time_to_first_tokens: 0.125, inter_token_latency: 0.125, token_latency: 0.125,
    tokens_per_second: 8, ms_per_token: 0.125, seconds_per_token: 0.125, time_per_token: 0.125 };
  const attributes: Attributes = {};
  for (const [key, value] of Object.entries(metrics)) {
    for (const name of [key, key.toUpperCase(), key.replace(/_/g, "-"), `metrics.${key}`]) {
      expect(isCredentialName(name)).toBe(false);
      attributes[name] = value;
    }
  }
  const secret = "synthetic-r7-timing-secret";
  registerSecret(secret);
  for (const key of ["tokens_secret", "latency_token_key", "x-session-token", "token_per_second_key", "secret.time_to_first_token", "gen_ai.response.time_to_first_token.api_key"]) {
    expect(isCredentialName(key)).toBe(true);
    attributes[key] = secret;
  }
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  const original: any = { _spanContext: ctx, name: "timing", kind: 2, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], attributes,
    status: { code: 1 }, resource: { attributes }, instrumentationScope: { name: "app", attributes },
    events: [{ name: "event", attributes }], links: [{ context: ctx, attributes }] };
  const before = JSON.stringify(original), clean = privacyFilter().span(original);
  for (const surface of [clean.attributes, clean.resource.attributes, (clean.instrumentationScope as any).attributes, clean.events[0].attributes!, clean.links[0].attributes!]) {
    for (const [key, value] of Object.entries(attributes)) expect(surface[key]).toBe(value === secret ? "<redacted>" : value);
  }
  expect(JSON.stringify(original)).toBe(before);
  // A protected name is not a privacy bypass for a secret or raw media value.
  expect(privacyFilter().attributes({ "gen_ai.response.time_to_first_token": secret, "gen_ai.response.latency": "iVBORw0K" })).toEqual({
    "gen_ai.response.time_to_first_token": "<redacted>", "gen_ai.response.latency": "<omitted: 8 base64 chars>",
  });
});
