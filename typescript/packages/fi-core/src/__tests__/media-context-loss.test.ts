import { trace, type Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { privacyFilter, rememberSpanValueLimit } from "../export-privacy";

// Short, synthetic base64 with no catalog magic signature.
const payload = "AQID".repeat(16);
const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; });

function snapshot(attributes: Attributes): any {
  return { _spanContext: ctx, name: "application", kind: 2, startTime: [1, 0], endTime: [2, 0], _duration: [1, 0], attributes,
    status: { code: 1 }, resource: { attributes: {} }, instrumentationScope: { name: "application" }, events: [], links: [] };
}

test.each(["", "transcript.", "id."])("RC2 count loss enters embedded JSON below '%s'", async boundary => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeCountLimit: 32 }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const key = "x.parts", value = JSON.stringify([{ [`${boundary}data`]: payload }]);
    const attrs = { [key]: value, ...Object.fromEntries(Array.from({ length: 31 }, (_, i) => [`filler.${i}`, i])), "x.mimeType": "audio/wav" };
    provider.getTracer("application").startSpan("embedded", { attributes: attrs }).end();
    const raw = exporter.getFinishedSpans()[0], before = JSON.stringify(raw.attributes);
    expect(raw.droppedAttributesCount).toBe(1); expect(raw.attributes[key]).toBe(value);
    const clean = privacyFilter().span(raw);
    expect(JSON.stringify(clean)).not.toContain(payload);
    expect(clean.attributes[key]).toBe("<unknown: host-truncated>");
    expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
    expect(Object.keys(clean.attributes).sort()).toEqual([...Object.keys(raw.attributes), "traceai.capture.host_data_unknown"].sort());
    for (const key of Object.keys(raw.attributes)) expect(clean.attributes[key]).toBe(key === "session.id" ? "<redacted>" : "<unknown: host-truncated>");
    expect(clean.name).toBe("<unknown: host-truncated>");
    expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
    for (const value of Object.values(clean.resource.attributes)) expect(value).toBe("<unknown: host-truncated>");
    expect(clean.status).toEqual({ code: raw.status.code });
    expect(clean.events).toEqual([]); expect(clean.links).toEqual([]);
    expect(clean.spanContext()).toMatchObject({ traceId: raw.spanContext().traceId, spanId: raw.spanContext().spanId });
    expect(clean.droppedAttributesCount).toBe(1);
    expect(JSON.stringify(raw.attributes)).toBe(before);
  } finally { await provider.shutdown(); }
});

test.each([48, 64, 80, 96])("RC2 value limit %i enters error prose and its incomplete JSON tail", async limit => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeValueLengthLimit: limit }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const message = `Bad request: {"inlineData":{"data":"${payload}","mimeType":"audio/wav"}}`;
    const attrs = { "error.message": message };
    const span = provider.getTracer("application").startSpan("error", { attributes: attrs, links: [{ context: ctx, attributes: attrs }] });
    span.addEvent("exception", { "exception.message": message }); span.end();
    const raw = exporter.getFinishedSpans()[0], before = JSON.stringify(raw.attributes);
    expect(raw.attributes["error.message"]).toBe(message.slice(0, limit));
    expect(String(raw.attributes["error.message"])).toContain("AQIDAQID");
    const clean = privacyFilter().span(raw);
    expect(JSON.stringify(clean)).not.toContain("AQIDAQID");
    expect(clean.attributes["error.message"]).toBe("<unknown: host-truncated>");
    expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
    expect(JSON.stringify(raw.attributes)).toBe(before);
  } finally { await provider.shutdown(); }
});

test.each(["attributes", "events", "links"])("RC2 %s loss propagates through balanced prose, nested JSON strings and arrays", site => {
  const object = { transcript: { id: { data: payload } } };
  const attrs = { "x.parts": JSON.stringify([JSON.stringify(object)]), "error.message": `Bad request: ${JSON.stringify(object)}` };
  const raw = snapshot(attrs); raw.events = [{ name: "exception", attributes: attrs }]; raw.links = [{ context: ctx, attributes: attrs }];
  if (site === "attributes") raw.droppedAttributesCount = 1;
  else raw[site][0].droppedAttributesCount = 1;
  const clean = privacyFilter().span(raw);
  expect(JSON.stringify(clean)).not.toContain(payload);
  const withheld = { "x.parts": "<unknown: host-truncated>", "error.message": "<unknown: host-truncated>" };
  expect(clean.attributes).toEqual({ ...withheld, "traceai.capture.host_data_unknown": true });
  expect(clean.events).toHaveLength(1); expect(clean.links).toHaveLength(1);
  expect(clean.events[0].attributes).toEqual(withheld); expect(clean.links[0].attributes).toEqual(withheld);
  expect(clean.events[0].name).toBe("<unknown: host-truncated>"); expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
  expect(clean.resource.attributes).toEqual({}); expect(clean.status).toEqual({ code: 1 });
  expect(site === "attributes" ? clean.droppedAttributesCount : (clean as any)[site][0].droppedAttributesCount).toBe(1);
  expect(raw.attributes).toEqual(attrs); expect(raw.events[0].attributes).toEqual(attrs); expect(raw.links[0].attributes).toEqual(attrs);
  expect(clean.spanContext()).toEqual(raw._spanContext);
});

test.each([120, 300])("RC2 mapped Genkit copy without limit slots omits incomplete media JSON at %i", limit => {
  const serialized = JSON.stringify({ file: { data: payload.repeat(4), mimeType: "audio/wav" } }).slice(0, limit);
  const raw = { ...snapshot({ "input.value": serialized, "input.mime_type": "application/json" }), _spanContext: { ...ctx } };
  raw.droppedAttributesCount = 0;
  const clean = privacyFilter().span(raw);
  expect(JSON.stringify(clean)).not.toContain("AQIDAQID");
  if (limit === 300) expect(clean.attributes["input.value"]).toContain("<omitted: 256 base64 chars>");
  expect(raw.attributes["input.value"]).toBe(serialized);
});

test.each(["data", "inlineData.data", "transcript.data", "id.data", "content", "bytes", "value", "audio"])(
  "RC2 unbalanced JSON is loss evidence for catalog field %s without SDK slots", key => {
    for (const prefix of ["", "Bad request: ", "Bad request: ["]) {
      const raw = snapshot({ "error.message": `${prefix}{${JSON.stringify(key)}:"${payload.slice(0, 12)}` });
      expect(JSON.stringify(privacyFilter().span(raw))).not.toContain("AQIDAQID");
    }
  });

test.each(["spanLimits", "generalLimits"].flatMap(source => [true, false].map(recorded => ({ source, recorded }))))("RC2 code-only $source survives a mapping copy without SDK slots (recorded=$recorded)", async ({ source, recorded }) => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ [source]: { attributeValueLengthLimit: 120 }, spanProcessors: [{
    onStart: span => { if (recorded) rememberSpanValueLimit(span); }, onEnd() {}, async forceFlush() {}, async shutdown() {},
  }, new SimpleSpanProcessor(exporter)] });
  if (!recorded) expect(trace.setGlobalTracerProvider(provider)).toBe(true);
  try {
    provider.getTracer("genkit-tracer").startSpan("mapped", { attributes: { "x.parts": JSON.stringify([{ data: payload }]), sentinel: "z".repeat(121) } }).end();
    const original = exporter.getFinishedSpans()[0];
    const copy = { ...snapshot(original.attributes), _spanContext: original.spanContext(), droppedAttributesCount: original.droppedAttributesCount };
    expect(copy._attributeValueLengthLimit).toBeUndefined();
    const clean = privacyFilter().span(copy);
    expect(JSON.stringify(clean)).not.toContain(payload);
    expect(clean.attributes["x.parts"]).toBe("<unknown: host-truncated>");
    expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
    expect(original.attributes.sentinel).toHaveLength(120);
  } finally { if (!recorded) trace.disable(); await provider.shutdown(); }
});

test.each([`{"blob":"${payload}`, `{"d\\u0061ta":"${payload}`, `["${payload}`])("RC2 malformed JSON without SDK slots fails closed: %s", tail => {
  expect(JSON.stringify(privacyFilter().span(snapshot({ "error.message": `Bad request: ${tail}` })))).not.toContain("AQIDAQID");
});

test.each(["count", "value"])("RC2 %s loss enters quoted JSON scalars and URL query values", loss => {
  const attrs = { "x.parts": JSON.stringify(payload), "error.message": `See https://example.invalid/?transcript.data=${payload}&mimeType=application/octet-stream` };
  const raw = snapshot(attrs);
  if (loss === "count") raw.droppedAttributesCount = 1;
  else raw._attributeValueLengthLimit = attrs["error.message"].length;
  expect(JSON.stringify(privacyFilter().span(raw))).not.toContain(payload);
  expect(raw.attributes).toEqual(attrs);
});

test.each(["attributes", "events", "links"].flatMap(site => [120, 300].flatMap(limit => [false, true].map(array => ({ site, limit, array }))))) (
  "RC2 native $site loss survives shorter mapped values at $limit (array=$array)", async ({ site, limit, array }) => {
    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanLimits: { attributeValueLengthLimit: limit }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
    try {
      const prefix = "/synthetic/local/home/fixture-user/clip.wav ";
      const message = `${prefix}https://example.invalid/u?data=${payload.repeat(8)}&mimeType=audio/wav`;
      const span = provider.getTracer("genkit-tracer").startSpan("rewritten");
      // SDK 2.0 does not truncate events/links; pin their post-SDK representation
      // here, with SDK 2.7's actual truncation exercised by the real-flow test.
      const attrs = { "exception.message": array ? [message.slice(0, limit)] : message.slice(0, limit) };
      if (site === "attributes") span.setAttributes(attrs);
      else if (site === "events") span.addEvent("exception", attrs);
      else span.addLink({ context: ctx, attributes: attrs });
      span.end();
      const original = exporter.getFinishedSpans()[0], before = JSON.stringify([original.attributes, original.events, original.links]);
      rememberSpanValueLimit(original);
      const shortened = message.slice(0, limit).replace(prefix, "~/clip.wav ");
      expect(shortened.length).toBeLessThan(limit); expect(shortened).toContain("AQIDAQID");
      const mapped = { "exception.message": array ? [shortened] : shortened };
      const copy = { ...snapshot(site === "attributes" ? mapped : {}), _spanContext: original.spanContext(),
        events: site === "events" ? [{ name: "exception", attributes: mapped }] : [],
        links: site === "links" ? [{ context: ctx, attributes: mapped }] : [] };
      // Genkit invokes onStart with the copy too: that must not clear native loss.
      rememberSpanValueLimit(copy);
      expect(JSON.stringify(privacyFilter().span(copy))).not.toContain("AQIDAQID");
      expect(JSON.stringify([original.attributes, original.events, original.links])).toBe(before);
    } finally { await provider.shutdown(); }
  });

test.each(["attributes", "events", "links"])("RC2 remembers native %s count loss even if a mapper discards the counter", site => {
  const original = { ...snapshot({}), _spanContext: { ...ctx } };
  if (site === "attributes") original.droppedAttributesCount = 1;
  else original[site] = [{ droppedAttributesCount: 1, attributes: {} }];
  rememberSpanValueLimit(original);
  const copy = { ...snapshot({ "x.parts": JSON.stringify([{ data: payload }]) }), _spanContext: original._spanContext };
  rememberSpanValueLimit(copy);
  expect(JSON.stringify(privacyFilter().span(copy))).not.toContain(payload);
});

test("RC2 unattested copies scrub shortened URL queries but preserve complete non-URL JSON", () => {
  process.env.OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT = "300";
  const message = `~/clip.wav https://example.invalid/u?data=${payload}`;
  const ordinary = JSON.stringify({ content: "ordinary prompt", data: "not classified as media" });
  const original = snapshot({ "output.value": ordinary, "exception.message": message });
  original.events = [{ name: "exception", attributes: { "exception.message": [message] } }];
  original.links = [{ context: ctx, attributes: { "error.message": message } }];
  const clean = privacyFilter().span(original);
  // Q8a/B-004: observe explicit loss before checking the shared-context target.
  const inheritedLoss = privacyFilter().span({ ...original, droppedAttributesCount: 1 });
  expect(inheritedLoss.attributes["output.value"]).toBe("<unknown: host-truncated>");
  expect(inheritedLoss.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  expect(JSON.stringify(clean)).not.toContain(payload);
  expect(original.attributes["exception.message"]).toBe(message);
});

test("RC2 complete JSON retains unmarked data and metadata text without loss", () => {
  const message = JSON.stringify({ data: payload, audio: { transcript: { content: "spoken words" } } });
  const lostJson = privacyFilter().span({ ...snapshot({ "x.parts": message }), droppedAttributesCount: 1 });
  expect(lostJson.attributes["x.parts"]).toBe("<unknown: host-truncated>");
  expect(lostJson.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  const inheritedProse = privacyFilter().span(snapshot({ "error.message": "Bad request: [ordinary prose" }));
  expect(inheritedProse.attributes["error.message"]).toBe("<unknown: host-truncated>");
  expect(inheritedProse.attributes["traceai.capture.host_data_unknown"]).toBe(true);
});

// G3-2 complete-capture target; owner: fi-observe; 2026-11-05; Q7a interim. NOT_RUN.
test.skip.each([48, 64, 80, 96])("G3-2 error prose: RC2 value limit %i enters error prose and its incomplete JSON tail", async limit => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeValueLengthLimit: limit }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const message = `Bad request: {"inlineData":{"data":"${payload}","mimeType":"audio/wav"}}`;
    const attrs = { "error.message": message };
    const span = provider.getTracer("application").startSpan("error", { attributes: attrs, links: [{ context: ctx, attributes: attrs }] });
    span.addEvent("exception", { "exception.message": message }); span.end();
    const raw = exporter.getFinishedSpans()[0], before = JSON.stringify(raw.attributes);
    expect(raw.attributes["error.message"]).toBe(message.slice(0, limit));
    expect(String(raw.attributes["error.message"])).toContain("AQIDAQID");
    const clean = privacyFilter().span(raw);
    expect(JSON.stringify(clean)).not.toContain("AQIDAQID");
    expect(clean.attributes["error.message"]).toContain("Bad request: ");
    expect(JSON.stringify(raw.attributes)).toBe(before);
  } finally { await provider.shutdown(); }
});

// G3-2 complete-capture target; owner: fi-observe; 2026-11-05; Q7a interim. NOT_RUN.
test.skip.each(["spanLimits", "generalLimits"].flatMap(source => [true, false].map(recorded => ({ source, recorded }))))("G3-2 mapped MIME: RC2 code-only $source survives a mapping copy without SDK slots (recorded=$recorded)", async ({ source, recorded }) => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ [source]: { attributeValueLengthLimit: 120 }, spanProcessors: [{
    onStart: span => { if (recorded) rememberSpanValueLimit(span); }, onEnd() {}, async forceFlush() {}, async shutdown() {},
  }, new SimpleSpanProcessor(exporter)] });
  if (!recorded) expect(trace.setGlobalTracerProvider(provider)).toBe(true);
  try {
    provider.getTracer("genkit-tracer").startSpan("mapped", { attributes: { "x.parts": JSON.stringify([{ data: payload }]), sentinel: "z".repeat(121) } }).end();
    const original = exporter.getFinishedSpans()[0];
    const copy = { ...snapshot(original.attributes), _spanContext: original.spanContext(), droppedAttributesCount: original.droppedAttributesCount };
    expect(copy._attributeValueLengthLimit).toBeUndefined();
    const clean = privacyFilter().span(copy);
    expect(JSON.stringify(clean)).not.toContain(payload);
    expect(clean.attributes["x.parts"]).toContain("<omitted: 64 base64 chars>");
    expect(original.attributes.sentinel).toHaveLength(120);
  } finally { if (!recorded) trace.disable(); await provider.shutdown(); }
});
