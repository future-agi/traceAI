import type { Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { FITracer } from "../trace/trace-config/FITracer";
import { privacyFilter } from "../export-privacy";
import { projectAttributes } from "../attribute-projection";

const payload = "AQID", ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; });
function sample(attributes: Attributes): any {
  return { _spanContext: ctx, name: "application", kind: 2, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], attributes,
    status: { code: 1 }, resource: { attributes: {} }, instrumentationScope: { name: "application" }, events: [], links: [] };
}
function longMessages(limit: number, boundary: string, mime = "mimeType"): { attributes: Attributes; key: string } {
  const parent = `gen_ai.input.messages.${limit - 1}.parts.0${boundary}.inlineData`;
  const key = `${parent}.data`;
  return { key, attributes: { ...Object.fromEntries(Array.from({ length: limit - 1 }, (_, i) => [`gen_ai.input.messages.${i}.content`, "ordinary words"])),
    [key]: payload, ...Object.fromEntries(Array.from({ length: 150 }, (_, i) => [`gen_ai.input.messages.${limit + i}.content`, "later words"])), [`${parent}.${mime}`]: "application/octet-stream" } };
}

test.each([undefined, 32, 128, 140].flatMap(limit => ["", ".transcript", ".id"].map(boundary => ({ limit, boundary }))))(
  "RC2 native count $limit loses deep MIME below '$boundary' but detached export never leaks", async ({ limit, boundary }) => {
    const exporter = new InMemorySpanExporter(), provider = new BasicTracerProvider({ spanLimits: limit === undefined ? undefined : { attributeCountLimit: limit }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
    try {
      const { attributes, key } = longMessages(limit ?? 128, boundary);
      provider.getTracer("host").startSpan("application", { attributes }).end();
      const original = exporter.getFinishedSpans()[0], before = JSON.stringify(original.attributes);
      expect(original.attributes[key]).toBe(payload); expect(original.droppedAttributesCount).toBeGreaterThan(0);
      const clean = privacyFilter().span(original);
      expect(clean.attributes[key]).toBe("<unknown: host-truncated>");
      expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
      expect(Object.keys(clean.attributes).sort()).toEqual([...Object.keys(original.attributes), "traceai.capture.host_data_unknown"].sort());
      for (const key of Object.keys(original.attributes)) expect(clean.attributes[key]).toBe(key === "session.id" ? "<redacted>" : "<unknown: host-truncated>");
      expect(clean.name).toBe("<unknown: host-truncated>");
      expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
      for (const value of Object.values(clean.resource.attributes)) expect(value).toBe("<unknown: host-truncated>");
      expect(clean.status).toEqual({ code: original.status.code });
      expect(clean.events).toEqual([]); expect(clean.links).toEqual([]);
      expect(JSON.stringify(clean)).not.toContain(payload); expect(JSON.stringify(original.attributes)).toBe(before);
      expect(clean.spanContext()).toEqual(original.spanContext()); expect(clean.droppedAttributesCount).toBe(original.droppedAttributesCount);
    } finally { await provider.shutdown(); }
  });

test.each([32, 128, 140].flatMap(limit => ["mimeType", "mime_type", "media_type", "contentType"].map(mime => ({ limit, mime }))))(
  "RC2 complete admission precedes A7 projection at $limit with $mime", ({ limit, mime }) => {
    const { attributes } = longMessages(limit, ".transcript", mime), before = JSON.stringify(attributes);
    const clean = privacyFilter().admissionAttributes(attributes);
    expect(JSON.stringify(clean)).not.toContain(payload);
    expect(JSON.stringify(projectAttributes(clean, limit).attributes)).not.toContain(payload);
    expect(JSON.stringify(projectAttributes(attributes, limit).attributes)).not.toContain(payload);
    expect(JSON.stringify(attributes)).toBe(before);
  });

test.each([32, 128, 140].flatMap(limit => ["mimeType", "media_type", "type"].map(hint => ({ limit, hint }))))(
  "RC2 A7 retains an omission when $hint is projected out at $limit", ({ limit, hint }) => {
    // Spec A7 (2026-11-01) ranks the companion (output content) above unknown metadata,
    // so it is projected out only when output content itself overflows.
    const companion = hint === "type" ? "input_audio" : "application/octet-stream";
    const attributes = { "output.value": payload, ...Object.fromEntries(Array.from({ length: 180 }, (_, i) => [`output.a.${i}`, "ordinary words"])), [`output.${hint}`]: companion };
    const projected = projectAttributes(attributes, limit);
    expect(projected.attributes[`output.${hint}`]).toBeUndefined();
    expect(projected.attributes["output.value"]).toBe("<omitted: 4 base64 chars>");
    expect(projected.dropped).toBeGreaterThan(0); expect(attributes["output.value"]).toBe(payload);
    // With metadata filler the companion now stays with its (still omitted) payload.
    const kept = projectAttributes({ "output.value": payload, ...Object.fromEntries(Array.from({ length: 180 }, (_, i) => [`a.${i}`, "ordinary words"])), [`output.${hint}`]: companion }, limit);
    expect(kept.attributes[`output.${hint}`]).toBe(companion);
    expect(kept.attributes["output.value"]).toBe("<omitted: 4 base64 chars>");
  });

test.each([8, 16, 24].flatMap(limit => ["mimeType", "mime_type", "media_type", "type", "format"].map(hint => ({ limit, hint }))))(
  "RC2 FITracer removes media before value limit $limit separates $hint on all surfaces", async ({ limit, hint }) => {
    const exporter = new InMemorySpanExporter(), provider = new BasicTracerProvider({ spanLimits: { attributeValueLengthLimit: limit }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
    try {
      const tracer = new FITracer({ tracer: provider.getTracer("library") });
      const parent = hint === "format" ? "audio.id.input_audio" : "audio.transcript.part";
      const attributes = { [`${parent}.data`]: payload, [`${parent}.${hint}`]: hint === "type" ? "input_audio" : hint === "format" ? "pcm" : "application/octet-stream" };
      const before = JSON.stringify(attributes);
      const span = tracer.startSpan("application", { attributes, links: [{ context: ctx, attributes }] });
      span.setAttributes(attributes); span.addEvent("event", attributes); span.addLink({ context: ctx, attributes }); span.addLinks([{ context: ctx, attributes }]); span.end();
      const original = exporter.getFinishedSpans()[0];
      for (const surface of [original.attributes, ...original.events.map(e => e.attributes), ...original.links.map(l => l.attributes)]) expect(JSON.stringify(surface)).not.toContain(payload);
      expect(JSON.stringify(privacyFilter().span(original))).not.toContain(payload); expect(JSON.stringify(attributes)).toBe(before);
    } finally { await provider.shutdown(); }
  });

test.each([8, 16, 24].flatMap(limit => ["", ".transcript", ".id"].map(boundary => ({ limit, boundary }))))(
  "RC2 native value limit $limit cannot bless a MIME prefix below '$boundary'", async ({ limit, boundary }) => {
    const exporter = new InMemorySpanExporter(), provider = new BasicTracerProvider({ spanLimits: { attributeValueLengthLimit: limit }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
    try {
      const parent = `part${boundary}`, attributes = { [`${parent}.data`]: payload, [`${parent}.mimeType`]: "application/octet-stream; encoding=binary" };
      provider.getTracer("host").startSpan("application", { attributes }).end();
      const original = exporter.getFinishedSpans()[0]; expect(original.attributes[`${parent}.data`]).toBe(payload);
      const clean = privacyFilter().span(original); expect(JSON.stringify(clean)).not.toContain(payload);
      expect(clean.attributes[`${parent}.data`]).toBe("<unknown: host-truncated>");
      expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
    } finally { await provider.shutdown(); }
  });

test.each(["attributes", "events", "links"])("RC2 %s count loss fails closed across indexed media and metadata boundaries", site => {
  const attributes = { "audio.transcript.content.0": payload, "audio.id.bytes.chunk": [1, 2, 3], "part[7].inlineData.data[0]": [payload], arbitrary: "iVBORw0K", "gen_ai.usage.data": payload, "gen_ai.usage.input_tokens": 7, "gen_ai.provider.name": "openai" };
  const original = sample(attributes); original.events = [{ name: "event", attributes }]; original.links = [{ context: ctx, attributes }];
  if (site === "attributes") original.droppedAttributesCount = 1;
  else original[site][0].droppedAttributesCount = 1;
  const before = JSON.stringify(original), clean = privacyFilter().span(original);
  for (const surface of [clean.attributes, clean.events[0].attributes!, clean.links[0].attributes!]) {
    expect(JSON.stringify(surface)).not.toContain(payload);
    for (const key of ["audio.transcript.content.0", "audio.id.bytes.chunk", "part[7].inlineData.data[0]", "arbitrary", "gen_ai.usage.data", "gen_ai.provider.name"]) expect(surface[key]).toBe("<unknown: host-truncated>");
    expect(surface["gen_ai.usage.input_tokens"]).toBe(7);
  }
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.events).toHaveLength(1); expect(clean.links).toHaveLength(1);
  expect(clean.events[0].name).toBe("<unknown: host-truncated>");
  expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
  expect(clean.resource.attributes).toEqual({}); expect(clean.status).toEqual({ code: 1 });
  expect(site === "attributes" ? clean.droppedAttributesCount : (clean as any)[site][0].droppedAttributesCount).toBe(1);
  expect(clean.spanContext()).toEqual(original._spanContext);
  expect(JSON.stringify(original)).toBe(before);
});

test("RC2 complete text MIME remains useful without loss; lossy media paths never trust it", () => {
  const attributes = { "part.content": payload, "part.mime_type": "text/plain" };
  // Q8a/B-004: make inherited-loss intent explicit, independent of test order.
  const countLoss = privacyFilter().span({ ...sample(attributes), droppedAttributesCount: 1 });
  expect(countLoss.attributes["part.content"]).toBe("<unknown: host-truncated>");
  expect(countLoss.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  const repeatedLoss = privacyFilter().span({ ...sample(attributes), droppedAttributesCount: 1 });
  expect(repeatedLoss.attributes["part.content"]).toBe("<unknown: host-truncated>");
  expect(repeatedLoss.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  const valueLoss = privacyFilter().span({ ...sample(attributes), _attributeValueLengthLimit: 10 });
  expect(valueLoss.attributes["part.content"]).toBe("<unknown: host-truncated>");
  expect(valueLoss.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  const intact = { "audio.transcript.content": "spoken words", "audio.id": "reference" };
  const inheritedLoss = privacyFilter().span(sample(intact));
  expect(inheritedLoss.attributes).toEqual({ "audio.transcript.content": "<unknown: host-truncated>", "audio.id": "<unknown: host-truncated>", "traceai.capture.host_data_unknown": true });
});

test.each([8, 16, 24])("RC2 complete pre-admission text evidence survives limit %i but is invalidated by later writes", async limit => {
  const exporter = new InMemorySpanExporter(), provider = new BasicTracerProvider({ resource: resourceFromAttributes({ "service.name": "svc" }), spanLimits: { attributeCountLimit: 32, attributeValueLengthLimit: limit }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const span = new FITracer({ tracer: provider.getTracer("library") }).startSpan("text", { attributes: { "output.value": "ordinary answer", "output.mime_type": "text/plain", ...Object.fromEntries(Array.from({ length: 150 }, (_, i) => [`gen_ai.input.messages.${i}.content`, "ordinary words"])) } });
    span.end(); const raw = exporter.getFinishedSpans()[0];
    const clean = privacyFilter().span(raw);
    if (limit === 8) {
      expect(clean.attributes["output.value"]).toBe("<unknown: host-truncated>");
      expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
    } else expect(clean.attributes["output.value"]).toBe("ordinary answer".slice(0, limit));
    raw.attributes["output.value"] = payload;
    const rewritten = privacyFilter().span(raw);
    if (limit === 8) {
      expect(rewritten.attributes["output.value"]).toBe("<unknown: host-truncated>");
      expect(rewritten.attributes["traceai.capture.host_data_unknown"]).toBe(true);
    } else expect(rewritten.attributes["output.value"]).toBe("<omitted: 4 base64 chars>");
  } finally { await provider.shutdown(); }
});

test("RC2 host annotations cannot supply pre-admission MIME evidence or exempt descendants", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeValueLengthLimit: 8 }, spanProcessors: [{ onStart(span) { span.setAttributes({ "part.mime_type": "application/octet-stream", "part.data": payload }); }, onEnd() {}, async shutdown() {}, async forceFlush() {} }, new SimpleSpanProcessor(exporter)] });
  try {
    new FITracer({ tracer: provider.getTracer("library") }).startSpan("text").end();
    expect(JSON.stringify(privacyFilter().span(exporter.getFinishedSpans()[0]))).not.toContain(payload);
    const raw = sample({ "part.mime_type": "text/plain", "part.inlineData.data": payload, "part.data": payload, "part.mime_type.0": "text/plain" }); raw.droppedAttributesCount = 1;
    expect(JSON.stringify(privacyFilter().span(raw))).not.toContain(payload);
  } finally { await provider.shutdown(); }
});

test("RC2 projection budget exhaustion omits content rather than throwing into the application", () => {
  const attributes = Object.fromEntries(Array.from({ length: 4097 }, (_, i) => [`field.${i}`, "ordinary words"]));
  expect(projectAttributes(attributes, 32)).toEqual({ attributes: { "traceai.capture.dropped_content_count": 4097 }, dropped: 4097 });
  expect(projectAttributes({ samples: Array(18000).fill("ordinary words") }, 32).attributes.samples).toBe("<omitted: media>");
});

test("RC2 truncated JSON, arrays, absent MIME, and non-base64 payloads fail closed without invoking accessors", () => {
  const execute = jest.fn(() => { throw new Error("user code"); });
  const attributes = Object.defineProperty({ json: '{"x":"AQ', arbitrary: [payload], "parts.0.bytes.0": 7, "parts.1.data": "binary bytes", "gen_ai.usage.input_tokens": 5 }, "mimeType", { enumerable: true, get: execute });
  const clean = privacyFilter().span({ ...sample(attributes), _attributeValueLengthLimit: 8 });
  expect(clean.attributes.json).toBe("<unknown: host-truncated>"); expect(clean.attributes.arbitrary).toBe("<unknown: host-truncated>");
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  expect(clean.attributes["parts.0.bytes.0"]).toBe("<unknown: host-truncated>"); expect(clean.attributes["parts.1.data"]).toBe("<unknown: host-truncated>");
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  expect(execute).not.toHaveBeenCalled();
});

// G3-2 complete-capture target; owner: fi-observe; 2026-11-05; Q7a interim. NOT_RUN.
test.skip.each([8, 16, 24])("G3-2 pre-admission text: RC2 complete pre-admission text evidence survives limit %i but is invalidated by later writes", async limit => {
  const exporter = new InMemorySpanExporter(), provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: 32, attributeValueLengthLimit: limit }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const span = new FITracer({ tracer: provider.getTracer("library") }).startSpan("text", { attributes: { "output.value": "ordinary answer", "output.mime_type": "text/plain", ...Object.fromEntries(Array.from({ length: 150 }, (_, i) => [`gen_ai.input.messages.${i}.content`, "ordinary words"])) } });
    span.end(); const raw = exporter.getFinishedSpans()[0];
    expect(privacyFilter().span(raw).attributes["output.value"]).toBe("ordinary answer".slice(0, limit));
    raw.attributes["output.value"] = payload;
    expect(privacyFilter().span(raw).attributes["output.value"]).toBe("<omitted: 4 base64 chars>");
  } finally { await provider.shutdown(); }
});

// G3-2 complete-capture target; owner: fi-observe; 2026-11-05; Q7a interim. NOT_RUN.
test.skip("G3-2 structured media: RC2 truncated JSON, arrays, absent MIME, and non-base64 payloads fail closed without invoking accessors", () => {
  const execute = jest.fn(() => { throw new Error("user code"); });
  const attributes = Object.defineProperty({ json: '{"x":"AQ', arbitrary: [payload], "parts.0.bytes.0": 7, "parts.1.data": "binary bytes", "gen_ai.usage.input_tokens": 5 }, "mimeType", { enumerable: true, get: execute });
  const clean = privacyFilter().span({ ...sample(attributes), _attributeValueLengthLimit: 8 });
  expect(clean.attributes.json).toBe("<omitted: media>"); expect(clean.attributes.arbitrary).toEqual(["<omitted: 4 base64 chars>"]);
  expect(clean.attributes["parts.0.bytes.0"]).toBe("<omitted: media>"); expect(clean.attributes["parts.1.data"]).toBe("<omitted: media>");
  expect(execute).not.toHaveBeenCalled();
});
