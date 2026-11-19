import type { Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { SemanticConventions, SKILL_LOAD_SPAN_NAME, CONTEXT_LOAD_SPAN_NAME } from "@traceai/fi-semantic-conventions";
import { isProjectionMetadata, projectAttributes, protectedFamily } from "../attribute-projection";
import { privacyFilter } from "../export-privacy";
import { FITracer } from "../trace/trace-config/FITracer";

const id = "11111111-1111-4111-8111-111111111111";
const version = "22222222-2222-4222-8222-222222222222";
const receipt = "33333333-3333-4333-8333-333333333333";
const digest = `sha256:${"a".repeat(64)}`;
const attrs: Attributes = {
  "fi.skill.id": id, "fi.skill.name": "refund-policy", "fi.skill.version_id": version,
  "fi.skill.version": 3, "fi.skill.digest": digest, "fi.skill.requested_label": "production",
  "fi.skill.requested_version": 3, "fi.skill.load.via": "sdk.read_file", "fi.skill.load.evidence": "delivered",
  "fi.skill.load.cached": true, "fi.skill.load.root_file": false, "fi.skill.files": 2, "fi.skill.bytes": 1234,
  "fi.skill.via_context.version_id": version,
  "fi.skills.version_ids": [version], "fi.skills.ids": [id], "fi.skills.refs": ["refund-policy@v3"],
  "fi.skills.count": 1, "fi.skills.truncated": true,
  "fi.context.id": id, "fi.context.version_id": version, "fi.context.digest": digest,
  "fi.context.name": "support-agent", "fi.context.version": 4, "fi.context.requested_label": "production",
  "fi.context.binding_id": id, "fi.context.session_id": receipt, "fi.context.linked_skill_version_ids": [version],
  "gen_ai.agent.name": "refund-agent", "gen_ai.agent.id": id,
};
const exportedAttrs = { ...attrs, "fi.context.session_id": "<redacted>" };
const env = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...env }; });

test("semantic constants cover the full runtime attribute schema", () => {
  const values = Object.values(SemanticConventions);
  for (const key of Object.keys(attrs)) expect(values).toContain(key);
  expect(values).toContain("error.type");
  expect(SKILL_LOAD_SPAN_NAME).toBe("skill.load");
  expect(CONTEXT_LOAD_SPAN_NAME).toBe("context.load");
});

test.each([false, true])("runtime identity survives admission and detached export with hidden content=%s", async hide => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const options = { hideInputs: hide, hideOutputs: hide, hideEmbeddingVectors: hide, hideInputImages: hide };
  try {
    const tracer = new FITracer({ tracer: provider.getTracer("runtime-test"), traceConfig: options });
    tracer.startSpan("skill.load", { attributes: attrs }).end();
    const native = exporter.getFinishedSpans()[0];
    expect(native.attributes).toEqual(attrs);
    const first = privacyFilter(options).span(native);
    expect(first.attributes).toEqual(exportedAttrs);
    expect(privacyFilter(options).span(first).attributes).toEqual(exportedAttrs);
    expect(native.attributes).toEqual(attrs);
  } finally { await provider.shutdown(); }
});

test("runtime identity is reserved ahead of content and is not arbitrary-base64 data after projection loss", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const crowded: Attributes = { ...attrs };
    for (let i = 0; i < 150; i++) crowded[`gen_ai.input.messages.${i}.content`] = `message ${i}`;
    for (const key of Object.keys(attrs)) {
      expect(protectedFamily(key)).toBe(1);
      expect(isProjectionMetadata(key)).toBe(false); // priority alone confers no privacy exemption
    }
    expect(projectAttributes(crowded, 128).attributes).toMatchObject(attrs);
    new FITracer({ tracer: provider.getTracer("runtime-test") }).startSpan("skill.load", { attributes: crowded }).end();
    const exported = privacyFilter().span(exporter.getFinishedSpans()[0]);
    expect(exported.attributes).toMatchObject(exportedAttrs);
    expect(isProjectionMetadata("fi.skill.contents")).toBe(false);
    expect(protectedFamily("fi.context.secret")).toBeUndefined();
  } finally { await provider.shutdown(); }
});

test("receipt name exemption is exact and typed; credential values still redact", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const secret = "44444444-4444-4444-8444-444444444444";
  try {
    const tracer = provider.getTracer("runtime-test");
    tracer.startSpan("skill.load", { attributes: {
      "fi.context.session_id": secret, "fi.context.name": secret,
      "fi.context.session_token": "private-session-token", "fi.skill.api_key": "private-api-key",
    } }).end();
    const clean = privacyFilter({}, [secret]).span(exporter.getFinishedSpans()[0]).attributes;
    expect(clean["fi.context.session_id"]).toBe("<redacted>");
    expect(clean["fi.context.name"]).toBe("<redacted>");
    expect(clean["fi.context.session_token"]).toBe("<redacted>");
    expect(clean["fi.skill.api_key"]).toBe("<redacted>");
    // The shared secret registry admits values of at least eight characters.
    tracer.startSpan("skill.load", { attributes: { "fi.skill.bytes": 12345678 } }).end();
    expect(privacyFilter({}, ["12345678"]).span(exporter.getFinishedSpans()[1]).attributes["fi.skill.bytes"]).toBe("<redacted>");
    tracer.startSpan("context.load", { attributes: { "fi.context.session_id": "not-a-receipt-uuid" } }).end();
    expect(privacyFilter().span(exporter.getFinishedSpans()[2]).attributes["fi.context.session_id"]).toBe("<redacted>");
  } finally { await provider.shutdown(); }
});

// A UUID-shaped value alone is not proof that a foreign session is a Hub receipt.
test("explicit span attributes are not verified Hub session receipts", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const native = provider.getTracer("runtime-test");
    const foreignId = "55555555-5555-4555-8555-555555555555";
    native.startSpan("foreign", { attributes: { "fi.context.session_id": foreignId, note: `receipt ${foreignId}` } }).end();
    new FITracer({ tracer: native }).startSpan("context.load", { attributes: { "fi.context.session_id": receipt, note: `receipt ${receipt}` } }).end();
    const [foreign, authored] = exporter.getFinishedSpans();
    const filtered = privacyFilter().span(foreign);
    expect(filtered.attributes["fi.context.session_id"]).toBe("<redacted>");
    expect(JSON.stringify(filtered)).not.toContain(foreignId);
    const explicit = privacyFilter().span(authored);
    expect(explicit.attributes["fi.context.session_id"]).toBe("<redacted>");
    expect(JSON.stringify(explicit)).not.toContain(receipt);
  } finally { await provider.shutdown(); }
});


test.each(["fi.skill.mime_type", "fi.modality"])("explicit %s takes precedence over a numeric byte count", async key => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const tracer = provider.getTracer("bytes");
    for (const [i, count] of [83451627, 84351627].entries()) {
      tracer.startSpan("media", { attributes: { "fi.skill.bytes": count, [key]: key.endsWith("modality") ? "image" : "audio/wav" } }).end();
      const raw = exporter.getFinishedSpans()[i];
      const view = privacyFilter({}, i ? [String(count)] : []).span(raw);
      expect(view.attributes["fi.skill.bytes"]).toBe(i ? "<redacted>" : "<omitted: media>");
      expect(JSON.stringify(view)).not.toContain(String(count));
      expect(raw.attributes["fi.skill.bytes"]).toBe(count);
    }
  } finally { await provider.shutdown(); }
});

test("retaining malformed runtime keys does not certify their values after projection", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: 12 }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const encoded = "AQID".repeat(11);
  try {
    const attributes = { ...Object.fromEntries(Array.from({ length: 30 }, (_, i) => [`input.part.${i}`, "notes"])),
      "fi.skill.version": encoded, "fi.skills.version_ids": [encoded], "fi.skill.load.cached": encoded,
      "fi.context.id": encoded, "fi.skill.requested_label": encoded, "fi.skill.load.via": encoded, "fi.context.digest": encoded,
      "fi.skill.name": [encoded], "fi.skills.refs": encoded, "fi.context.name": "support-agent" };
    new FITracer({ tracer: provider.getTracer("projection") }).startSpan("skill.load", { attributes }).end();
    const raw = exporter.getFinishedSpans()[0];
    expect(raw.attributes["fi.skill.version"]).toBe(encoded);
    expect(raw.droppedAttributesCount).toBeGreaterThan(0);
    const first = privacyFilter().span(raw);
    for (const view of [first, privacyFilter().span(first)]) {
      expect(view.attributes["fi.context.name"]).toBe("support-agent");
      expect(JSON.stringify(view)).not.toContain(encoded);
    }
  } finally { await provider.shutdown(); }
});
