import { ROOT_CONTEXT, trace, type Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { privacyFilter } from "../export-privacy";
import { mediaReplacement, scrubAttribute, scrubValue } from "../privacy-runtime";
import { projectAttributes } from "../attribute-projection";
import { FITracer } from "../trace/trace-config/FITracer";
import { capBase64Images } from "../trace/trace-config/imageCap";
import { redactValue } from "../trace/trace-config/maskingRules";
import { generateTraceConfig } from "../trace/trace-config/traceConfig";
import { DEFAULT_BASE64_IMAGE_MAX_LENGTH, REDACTED_VALUE } from "../trace/trace-config/constants";
import { setSession } from "../trace/contextAttributes";
import { registerSecret } from "../secrets";

const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; });
const png = "iVBORw0KGgoAAAANSUhEUg" + "AQID".repeat(32);
const jpeg = "/9j/4AAQ" + "AQID".repeat(32);
function snapshot(attributes: Attributes): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { name: "retained span", kind: 2, spanContext: () => ctx, attributes,
    startTime: [1, 0], endTime: [2, 0], duration: [1, 0], status: { code: 1 },
    resource: { attributes: {} }, instrumentationScope: { name: "fixture" }, events: [], links: [] };
}

test.each(["spanLimits", "generalLimits"].flatMap(source => ["base64", "base64url"].flatMap(encoding =>
  ["attributes", "events", "links"].map(surface => ({ source, encoding, surface })))))
("I1-P1 code-only $source unrecorded $surface $encoding copy", async ({ source, encoding, surface }) => {
  // No magic: this payload needs the lost-context policy, not the easy signature path.
  const bytes = Buffer.from([251, 255, 250, 253, 254, 255, ...Array(42).fill(33)]);
  const payload = bytes.toString(encoding as "base64" | "base64url");
  expect(payload).toHaveLength(64);
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ [source]: { attributeValueLengthLimit: 120 }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  expect(trace.setGlobalTracerProvider(provider)).toBe(true);
  try {
    const attrs = { "x.parts": JSON.stringify([{ data: payload }]), sentinel: "z".repeat(121) };
    provider.getTracer("mapped").startSpan("native", { attributes: attrs }).end();
    const original = exporter.getFinishedSpans()[0];
    const copiedAttrs = { ...original.attributes };
    const copy = snapshot(surface === "attributes" ? copiedAttrs : {});
    copy.spanContext = () => original.spanContext();
    if (surface !== "attributes") copy[surface] = [{ name: "copy event", context: original.spanContext(), attributes: copiedAttrs, time: [1, 0] }];
    expect(copy._attributeValueLengthLimit).toBeUndefined();
    expect(copy._spanLimits).toBeUndefined();
    expect(copy._spanContext).toBeUndefined();
    expect(JSON.stringify(copy)).toContain(payload);
    const before = JSON.stringify(copy);
    const clean = privacyFilter({ hideInputImages: true, hideErrorDetails: false }).span(copy);
    expect(clean.name).toBe("<unknown: host-truncated>");
    const result = surface === "attributes" ? clean.attributes : (clean as any)[surface][0].attributes;
    expect(result).toEqual({ "x.parts": "<unknown: host-truncated>", sentinel: "<unknown: host-truncated>", ...(surface === "attributes" ? { "traceai.capture.host_data_unknown": true } : {}) });
    expect(clean.attributes).toEqual(surface === "attributes" ? result : { "traceai.capture.host_data_unknown": true });
    expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
    expect(clean.resource.attributes).toEqual({}); expect(clean.status).toEqual({ code: 1 });
    expect(clean.events).toHaveLength(surface === "events" ? 1 : 0);
    expect(clean.links).toHaveLength(surface === "links" ? 1 : 0);
    if (surface === "events") expect(clean.events[0].name).toBe("<unknown: host-truncated>");
    expect(clean.spanContext()).toMatchObject({ traceId: original.spanContext().traceId, spanId: original.spanContext().spanId });
    expect(original.attributes.sentinel).toHaveLength(120);
    expect(JSON.stringify(clean)).not.toContain(payload);
    expect(JSON.stringify(clean)).not.toContain(payload.slice(0, 20));
    // Check the actual serialized export bytes, with a populated raw control.
    const encodedBytes = Buffer.from(payload);
    expect(Buffer.from(before).includes(encodedBytes)).toBe(true);
    expect(Buffer.from(JSON.stringify(clean)).includes(encodedBytes)).toBe(false);
    expect(JSON.stringify(copy)).toBe(before);
  } finally { trace.disable(); await provider.shutdown(); }
});

test.each(["environment", "options"])("I1-P2 embedding hiding retains text: %s", mode => {
  if (mode === "environment") process.env.FI_HIDE_EMBEDDING_VECTORS = "true";
  const filter = privacyFilter(mode === "options" ? { hideEmbeddingVectors: true } : {});
  const input = JSON.stringify({ input: "public embedding prompt", nested: { embedding: [0.125, 0.875] } });
  const raw = snapshot({ "input.value": input, "output.value": JSON.stringify({ embeddings: [[0.125, 0.875]] }) });
  const clean = filter.span(raw);
  expect(clean.name).toBe("retained span");
  expect(JSON.parse(clean.attributes["input.value"] as string)).toEqual({ input: "public embedding prompt", nested: { embedding: REDACTED_VALUE } });
  expect(clean.attributes["output.value"]).toBe(REDACTED_VALUE);
  expect(JSON.stringify(clean)).not.toContain("0.875");
  expect(raw.attributes["input.value"]).toBe(input);
});

test("I1-P2 malformed and nested vector containers stay hidden", () => {
  const filter = privacyFilter({ hideEmbeddingVectors: true });
  const input = JSON.stringify({ input: "ordinary text", embeddings: [{ text: "ordinary item", values: [0.314159, 0.875] }] });
  const clean = filter.span(snapshot({ "input.value": input, partial: 'prefix {"embedding":[0.314159' }));
  expect(clean.name).toBe("retained span");
  expect(clean.attributes["input.value"]).toContain("ordinary text");
  expect(clean.attributes["input.value"]).toContain("ordinary item");
  expect(JSON.stringify(clean.attributes)).not.toContain("0.314159");
  expect(JSON.stringify(clean.attributes)).not.toContain("0.875");
});

test("I1-P3 valid nested JSON and hidden markers remain structurally intact", () => {
  const payload = { error: REDACTED_VALUE, audio: [REDACTED_VALUE], nested: JSON.stringify({ data: REDACTED_VALUE }) };
  const raw = JSON.stringify(JSON.stringify(payload));
  expect(JSON.parse(JSON.parse(scrubAttribute(raw)))).toEqual(payload);
  const media = JSON.stringify({ nested: JSON.stringify({ data: png }), password: "private-i1-secret" });
  const clean = scrubAttribute(media);
  expect(() => JSON.parse(clean)).not.toThrow();
  expect(clean).not.toContain(png);
  expect(clean).not.toContain("private-i1-secret");
  expect(scrubValue({ password: REDACTED_VALUE })).toEqual({ password: "<redacted>" });
});

test("I1-P4 projection preserves numeric media-named counters and zero deferral", () => {
  const attrs: Attributes = {
    "gen_ai.usage.input_tokens": 2, "gen_ai.usage.output_tokens": 3, "gen_ai.usage.total_tokens": 5,
    "gen_ai.usage.input_tokens.audio": 0, "gen_ai.usage.output_tokens.audio": 0,
    "gen_ai.audio.bytes": 9, "output.value": "ordinary answer", "output.mime_type": "text/plain",
  };
  expect(projectAttributes(attrs, 16).attributes).toEqual(attrs);
  expect(privacyFilter().span(snapshot(attrs)).attributes).toEqual(attrs);
  for (const n of [0, 9, 16777216]) expect(mediaReplacement(n, "gen_ai.audio.bytes")).toBeUndefined();
  expect(mediaReplacement([1, 2, 3], "audio")).toBe("<omitted: media>");
});

test.each([png, jpeg.replace(/\+/g, "-").replace(/\//g, "_"), `data:image/png;base64,${png}`])
("I1-P5 A1 error media is hidden independently of error-detail toggle: %s", async image => {
  // PENDING Z-OVER: F2 cap-0 whole-scope replacement precedes I1 media markers.
  for (const base64ImageMaxLength of [undefined, 32000]) {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const options = { hideInputImages: true, hideErrorDetails: false, ...(base64ImageMaxLength === undefined ? {} : { base64ImageMaxLength }) };
  try {
    const span = new FITracer({ tracer: provider.getTracer("errors"), traceConfig: options }).startSpan("image error");
    const message = `Cannot decode ${image} please retry`;
    span.recordException(new Error(message));
    span.setStatus({ code: 2, message });
    span.addEvent("plain", { note: "ordinary error text" });
    span.end();
    const native = exporter.getFinishedSpans()[0];
    for (const clean of [native, privacyFilter(options).span(native)]) {
      expect(clean.name).toBe("image error");
      expect(clean.events.filter(e => e.name === "exception")).toHaveLength(1);
      expect(clean.events.find(e => e.name === "plain")?.attributes?.note).toBe("ordinary error text");
      if (base64ImageMaxLength === undefined && image.startsWith("data:")) {
        expect(clean.status.message).toBe("[image-redacted]");
      } else {
      expect(clean.status.message).toContain("Cannot decode");
      }
      expect(JSON.stringify({ attributes: clean.attributes, events: clean.events, status: clean.status })).not.toContain(image);
      expect(JSON.stringify(clean.events)).not.toContain("AQIDAQID");
    }
  } finally { await provider.shutdown(); }
  }
});

test.each(["ordinary", "registered", "credential-shaped"])("I1-H1b typed identity keeps ordinary ids but not credential values (%s)", async kind => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  const id = kind === "registered" ? "arbitrary-session-credential-i1"
    : kind === "credential-shaped" ? "sk-" + "I".repeat(24) : "i1-ordinary-typed-correlation";
  const expected = kind === "ordinary" ? id : "<redacted>";
  try {
    const tracer = new FITracer({ tracer: provider.getTracer("session") });
    tracer.startActiveSpan("session", { attributes: { "input.value": id, "control.label": "i1-public-control" } }, setSession(ROOT_CONTEXT, { sessionId: id }), span => span.end());
    const native = exporter.getFinishedSpans()[0];
    const before = JSON.stringify(native.attributes);
    if (kind === "registered") {
      // Keep the original credential regression, with evidence independent of its name.
      expect(native.attributes["session.id"]).toBe(id);
      expect(native.attributes["input.value"]).toBe(id);
      registerSecret(id); // Discovered after admission; export must still scrub both copies.
    }
    const first = privacyFilter().span(native);
    for (const clean of [first, privacyFilter().span(first)]) {
      expect(clean.name).toBe("session");
      expect(clean.attributes["session.id"]).toBe(expected);
      expect(clean.attributes["input.value"]).toBe(expected);
      expect(clean.attributes["control.label"]).toBe("i1-public-control");
      if (kind !== "ordinary") expect(JSON.stringify(clean)).not.toContain(id);
    }
    // A distinct, ordinary unbound id is a grouping key and exports as set.
    const unboundId = `different-unbound-i1-${kind}`;
    const unbound = privacyFilter().span(snapshot({ "session.id": unboundId, "control.label": "i1-public-control" }));
    for (const clean of [unbound, privacyFilter().span(unbound)]) {
      expect(clean.attributes["session.id"]).toBe(unboundId);
      expect(clean.attributes["control.label"]).toBe("i1-public-control");
    }
    expect(JSON.stringify(native.attributes)).toBe(before);
  } finally { await provider.shutdown(); }
});

test("I1-S1 retains value-free streamed argument syntax, not truncated payloads", () => {
  for (const fragment of ['{"city":', '{"zone":', '{"arbitrary-field":']) expect(scrubValue(fragment)).toBe(fragment);
  expect(scrubValue(`{"data":"${png}`)).not.toContain(png);
  expect(scrubValue('{"password":"private-i1-secret"}')).not.toContain("private-i1-secret");
});

test("I1-S2 structural message hiding retains role and hides reasoning/tools", () => {
  const messages = JSON.stringify([{ role: "assistant", content: "private answer", reasoning: "private reasoning", tool_calls: [{ arguments: "private args" }] }]);
  const raw = snapshot({ "gen_ai.output.messages": messages, "output.value": messages, "raw.output": messages, "gen_ai.usage.total_tokens": 13 });
  const clean = privacyFilter({ hideOutputText: true }).span(raw);
  expect(JSON.parse(clean.attributes["gen_ai.output.messages"] as string)[0]).toEqual({ role: "assistant", content: REDACTED_VALUE, reasoning: REDACTED_VALUE, tool_calls: REDACTED_VALUE });
  expect(clean.attributes["gen_ai.usage.total_tokens"]).toBe(13);
  expect(JSON.stringify(clean.attributes)).not.toContain("private");
});

test.each([false, true])("I1-S1/S2 tool identity and bounded request metadata survive content hiding (hideInputs=%s)", hideInputs => {
  const tools = [{ type: "mcp", server_label: "ordinary", server_url: "https://example.invalid" }];
  const clean = privacyFilter({ hideInputs, hideOutputs: true }).span(snapshot({
    "gen_ai.tool.call.id": "ordinary-call", "gen_ai.tool.name": "read",
    "gen_ai.tool.definitions": JSON.stringify(tools),
    "gen_ai.request.parameters": JSON.stringify({ tools, prompt: { id: "prompt-id", version: "2" }, suffix: "private suffix" }),
  }));
  expect(clean.attributes["gen_ai.tool.call.id"]).toBe("ordinary-call");
  expect(clean.attributes["gen_ai.tool.name"]).toBe("read");
  // PENDING I1xF2-PRECEDENCE: upstream opaque input hiding follows I1 metadata reduction.
  if (hideInputs) {
    expect(clean.attributes["gen_ai.tool.definitions"]).toBe(REDACTED_VALUE);
    expect(clean.attributes["gen_ai.request.parameters"]).toBe(REDACTED_VALUE);
  } else {
  expect(JSON.parse(clean.attributes["gen_ai.tool.definitions"] as string)).toEqual(tools);
  expect(JSON.parse(clean.attributes["gen_ai.request.parameters"] as string).prompt).toEqual({ id: "prompt-id", version: "2" });
  }
  expect(JSON.stringify(clean)).not.toContain("private suffix");
});

test("I1-O1 cap mechanism has exact boundary while composed A remains stronger", () => {
  // PENDING K2: the image cap defaults to zero; 32000 remains an explicit option.
  expect(DEFAULT_BASE64_IMAGE_MAX_LENGTH).toBe(0);
  expect(generateTraceConfig({ base64ImageMaxLength: 32000 }).base64ImageMaxLength).toBe(32000);
  expect(generateTraceConfig().base64ImageMaxLength).toBe(DEFAULT_BASE64_IMAGE_MAX_LENGTH);
  expect(capBase64Images(png, png.length)).toBe(png);
  expect(capBase64Images(png, png.length - 1)).toBe(REDACTED_VALUE);
  const config = generateTraceConfig({ base64ImageMaxLength: png.length - 1, piiRedaction: true });
  expect(redactValue(config, png)).toBe(REDACTED_VALUE);
  const raw = snapshot({ arbitrary: png, safe: "ordinary words" });
  const clean = privacyFilter({ base64ImageMaxLength: png.length }).span(raw);
  expect(clean.name).toBe("retained span");
  expect(clean.attributes.safe).toBe("ordinary words");
  // PENDING I1xF2-PRECEDENCE T10: TS I1 media omission dominates positive caps.
  // Retain the original omission/absence checks.
  expect(clean.attributes.arbitrary).toBe("<omitted: 150 base64 chars>");
  expect(clean.attributes.arbitrary).toContain("<omitted:");
  expect(JSON.stringify(clean)).not.toContain(png);
  expect(JSON.stringify(clean)).not.toContain(png.slice(0, 22));
});

test.each([false, true])("I1-O1 composed event/exception/status PII=%s", async piiRedaction => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const options = { piiRedaction, hideErrorDetails: false, base64ImageMaxLength: 32 };
    const span = new FITracer({ tracer: provider.getTracer("pii"), traceConfig: options }).startSpan("pii span", { attributes: { safe: "ordinary words" } });
    const message = `Contact person@example.com ${png}`;
    span.addEvent("retry", { note: message, count: 2 });
    span.recordException(new Error(message));
    span.setStatus({ code: 2, message });
    span.end();
    const clean = privacyFilter(options).span(exporter.getFinishedSpans()[0]);
    expect(clean.name).toBe("pii span");
    expect(clean.attributes.safe).toBe("ordinary words");
    expect(clean.events.filter(e => e.name === "exception")).toHaveLength(1);
    expect(clean.events.find(e => e.name === "retry")?.attributes?.count).toBe(2);
    const details = JSON.stringify({ events: clean.events, status: clean.status });
    expect(details).not.toContain(png);
    expect(details.includes("person@example.com")).toBe(!piiRedaction);
    if (piiRedaction) expect(details).toContain("<EMAIL_ADDRESS>");
  } finally { await provider.shutdown(); }
});


test("I1xF2 exact whole-image marker is terminal only at the media boundary", () => {
  expect(mediaReplacement("[image-redacted]", "audio.data")).toBe("[image-redacted]");
  expect(scrubValue({ audio: ["[image-redacted]"], password: "[image-redacted]" })).toEqual({
    audio: ["[image-redacted]"], password: "<redacted>",
  });
  expect(scrubValue("[image-redacted]", ["[image-redacted]"])).toBe("<redacted>");
  expect(mediaReplacement("[image-redacted]AQID", "audio.data")).toBe("<omitted: media>");
  expect(mediaReplacement("AQID[image-redacted]", "audio.data")).toBe("<omitted: media>");
  expect(scrubValue({ audio: "AQIDBAUGBwgJCgsM", safe: "ordinary words" })).toEqual({
    audio: "<omitted: 16 base64 chars>", safe: "ordinary words",
  });
});
