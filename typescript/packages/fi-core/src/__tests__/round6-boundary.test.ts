import { SpanStatusCode } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { FITracer } from "../trace/trace-config/FITracer";
import { privacyFilter } from "../export-privacy";
import { registerSecret } from "../secrets";
import { DROPPED_CONTENT_COUNT, projectAttributes, PROJECTION_METADATA } from "../attribute-projection";

const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(?:FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; });
function fixture(): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, name: "application", kind: 2, startTime: [1, 2], endTime: [3, 4], _duration: [2, 2], droppedAttributesCount: 9,
    attributes: {}, status: { code: 2 }, events: [], links: [], resource: resourceFromAttributes({ safe: "keep" }), instrumentationScope: { name: "application" } };
}
test.each([16, 128])("A7 library late usage survives %i SDK slots with deterministic projection", async limit => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: limit }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const tracer = new FITracer({ tracer: provider.getTracer("library") });
  const span = tracer.startSpan("completion", { attributes: { "gen_ai.span.kind": "LLM", "traceai.capture.layer": "library" } });
  for (let i = 0; i < 150; i++) span.setAttribute(`gen_ai.input.messages.${i}.message.content`, "private message");
  span.setAttributes({ "output.value": "answer", "gen_ai.usage.input_tokens": 101, "gen_ai.usage.output_tokens": 7, "gen_ai.usage.total_tokens": 108 });
  span.setStatus({ code: SpanStatusCode.ERROR }); span.end(); await provider.forceFlush();
  const result = exporter.getFinishedSpans()[0], attrs = result.attributes;
  // Spec A7: protected fields are plain attributes (no capsule) and the content marker counts omitted content keys.
  expect(attrs).toMatchObject({ "gen_ai.span.kind": "LLM", "traceai.capture.layer": "library", "gen_ai.usage.input_tokens": 101, "gen_ai.usage.output_tokens": 7, "gen_ai.usage.total_tokens": 108, "error.type": "_OTHER" });
  expect(attrs).not.toHaveProperty([PROJECTION_METADATA]);
  expect(attrs["output.value"]).toBe("answer"); expect(attrs[DROPPED_CONTENT_COUNT]).toBeGreaterThan(0);
  expect(attrs["gen_ai.input.messages.149.message.content"]).toBe("private message"); expect(attrs).not.toHaveProperty(["gen_ai.input.messages.0.message.content"]);
  // Only content was omitted here, so OTel's all-attribute counter equals the content count.
  expect(result.droppedAttributesCount).toBe(attrs[DROPPED_CONTENT_COUNT]);
  expect(Object.keys(attrs).length).toBeLessThanOrEqual(limit); await provider.shutdown();
});
test("B2 SDK value truncation cannot export a prefix of a registered library credential", async () => {
  const secret = "k9V7h2qR4p8Z_SYNTHETIC_FI_CREDENTIAL"; registerSecret(secret);
  const exporter = new InMemorySpanExporter(), provider = new BasicTracerProvider({ spanLimits: { attributeValueLengthLimit: 12 }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const tracer = new FITracer({ tracer: provider.getTracer("library") });
  const span = tracer.startSpan("credential", { attributes: { initial: secret, "session.id": "app-session" } }); span.setAttribute("later", secret); span.setAttributes({ many: [secret] }); span.end();
  const result = exporter.getFinishedSpans()[0]; expect(result.attributes).toEqual({ initial: "<redacted>", later: "<redacted>", many: ["<redacted>"], "session.id": "app-session" });
  expect(privacyFilter().span(result).attributes["session.id"]).toBe("<redacted>");
  expect(JSON.stringify(result.attributes)).not.toContain(secret.slice(0, 12));
  const late = "late-transport-credential-SYNTHETIC_PRIVATE";
  const earlySpan = tracer.startSpan("discovered-after-start", { attributes: { initial: late } });
  registerSecret(late); earlySpan.end();
  expect(exporter.getFinishedSpans()[1].attributes.initial).toBe("<redacted>");
  await provider.shutdown();
});
test("S5 captured JSON is harvested before pre-admission scrubbing removes its credential fields", async () => {
  const secret = "captured-json-body-SYNTHETIC_PRIVATE";
  const exporter = new InMemorySpanExporter(), provider = new BasicTracerProvider({ spanLimits: { attributeValueLengthLimit: 12 }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const tracer = new FITracer({ tracer: provider.getTracer("library") });
  const span = tracer.startSpan("captured-body", { attributes: { "input.value": JSON.stringify({ database_password: secret }) } });
  span.setStatus({ code: 2, message: secret }); span.recordException(new Error(secret)); span.end();
  const native = exporter.getFinishedSpans()[0], before = JSON.stringify([native.attributes, native.status, native.events, native.links]);
  const clean = privacyFilter().span(native);
  expect(JSON.stringify(clean)).not.toContain(secret); expect(clean.status).toEqual({ code: 2, message: "<unknown: host-truncated>" });
  expect(clean.attributes["input.value"]).toBe("<unknown: host-truncated>");
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  for (const [key, value] of Object.entries(clean.attributes)) if (key !== "traceai.capture.host_data_unknown") expect(value).toBe(key === "session.id" ? "<redacted>" : "<unknown: host-truncated>");
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
  for (const value of Object.values(clean.resource.attributes)) expect(value).toBe("<unknown: host-truncated>");
  expect(clean.events).toHaveLength(1); expect(clean.links).toEqual([]);
  expect(clean.events[0].name).toBe("<unknown: host-truncated>");
  expect(clean.events[0].attributes?.["exception.message"]).toBe("<unknown: host-truncated>");
  for (const value of Object.values(clean.events[0].attributes!)) expect(value).toBe("<unknown: host-truncated>");
  expect(clean.spanContext()).toMatchObject({ traceId: native.spanContext().traceId, spanId: native.spanContext().spanId });
  expect(clean.droppedAttributesCount).toBe(native.droppedAttributesCount);
  expect(clean.events[0].droppedAttributesCount).toBe(native.events[0].droppedAttributesCount);
  expect(JSON.stringify([native.attributes, native.status, native.events, native.links])).toBe(before);
  await provider.shutdown();
});
test.each([
  { options: { hideInputs: true }, key: "llm.prompt_template.variables", value: "private prompt variables" },
  { options: { hideErrorDetails: true }, key: "exception.message", value: "private exception details" },
  { options: { hideInputImages: true }, key: "image.url", value: "https://example.invalid/private-image" },
])("B2 overflow metadata cannot bypass hiding for $key", ({ options, key, value }) => {
  const attrs = { ...Object.fromEntries(Array.from({ length: 40 }, (_, i) => [`gen_ai.usage.metric_${i}`, i])), [key]: value };
  const projected = projectAttributes(attrs, 16).attributes;
  expect(String(projected[PROJECTION_METADATA])).not.toContain(value);
  expect(JSON.stringify(privacyFilter(options).attributes(projected))).not.toContain(value);
  // Also protect older capsules and application-supplied capsules by original key.
  expect(JSON.stringify(privacyFilter(options).attributes({ [PROJECTION_METADATA]: JSON.stringify({ [key]: value }) }))).not.toContain(value);
});
test("B2 raw resource pair exhaustion fails closed instead of discarding late MIME hints", () => {
  const raw: [string, unknown][] = [["parts.0.inlineData.data", "U0VDUkVU"], ...Array.from({ length: 4095 }, (_, i): [string, unknown] => [`safe.${i}`, "keep"]), ["parts.0.inlineData.mimeType", "audio/wav"]];
  const original = { ...fixture(), resource: { _rawAttributes: raw } }, before = JSON.stringify(original);
  const clean = privacyFilter().span(original);
  expect(clean.resource.attributes).toEqual({}); expect(clean.attributes).toEqual({ "traceai.capture.host_data_unknown": true });
  expect(clean.name).toBe("<unknown: host-truncated>"); expect(clean.status).toEqual({ code: 2 });
  expect(clean.events).toEqual([]); expect(clean.links).toEqual([]);
  expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>" });
  expect(JSON.stringify(clean)).not.toContain("U0VDUkVU"); expect(clean.spanContext()).toEqual(original._spanContext);
  expect(clean.startTime).toEqual(original.startTime); expect(clean.droppedAttributesCount).toBe(9); expect(JSON.stringify(original)).toBe(before);
});
test("B2 environment-only image hiding protects status and exceptions exactly like explicit policy", () => {
  const url = "https://example.invalid/private-image";
  const original = { ...fixture(), attributes: { "image.url": url, "exception.message": url }, status: { code: 2, message: url }, events: [{ name: "exception", attributes: { "exception.message": url, "exception.stacktrace": url } }] };
  process.env.FI_HIDE_INPUT_IMAGES = "true";
  const fromEnv = privacyFilter().span(original); delete process.env.FI_HIDE_INPUT_IMAGES;
  const explicit = privacyFilter({ hideInputImages: true }).span(original);
  expect(JSON.stringify(fromEnv)).not.toContain(url); expect(JSON.stringify(fromEnv)).toBe(JSON.stringify(explicit));
  expect(original.status.message).toBe(url);
});
