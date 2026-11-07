import { type Attributes } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { privacyFilter, rememberSpanValueLimit } from "../export-privacy";
import { registerSecret } from "../secrets";

// Authored before the production overlay. RED/GREEN has not been executed.
const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; });
function fixture(attributes: Attributes = {}): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { _spanContext: ctx, spanContext: () => ctx, name: "native operation name", kind: 2, attributes,
    startTime: [1, 0], endTime: [2, 0], duration: [1, 0], ended: true,
    status: { code: 2 }, resource: { attributes: {} },
    instrumentationScope: { name: "native scope name", version: "version-longer-than-eight", schemaUrl: "https://example.invalid/schema" }, events: [], links: [] };
}

test.each(["direct", "spanLimits", "absent"])("R2 copied %s limit cannot bless shortened content on any FI surface", slot => {
  const native = fixture({ note: "cut/v1::" });
  if (slot === "spanLimits") native._spanLimits = { attributeValueLengthLimit: 8 };
  else native._attributeValueLengthLimit = 8;
  rememberSpanValueLimit(native);
  const mapped = { ...native, attributes: { note: "cut/v1:", "gen_ai.usage.total_tokens": 7 },
    status: { code: 2, message: "cut/v1:" }, resource: { attributes: { note: "cut/v1:" } },
    instrumentationScope: { ...native.instrumentationScope, attributes: { note: "cut/v1:" } },
    events: [{ name: "native event name", time: [1, 0], attributes: { "exception.message": "cut/v1:" } }],
    links: [{ context: native.spanContext(), attributes: { note: "cut/v1:" } }] };
  if (slot === "absent") delete mapped._attributeValueLengthLimit;
  // Mapping pipelines may call the observation hook again; loss stays sticky.
  rememberSpanValueLimit(mapped);
  const before = JSON.stringify(mapped), clean = privacyFilter().span(mapped);
  expect(native.attributes.note).toBe("cut/v1::");
  expect(clean.attributes.note).toBe("<unknown: host-truncated>");
  expect(clean.status.message).toBe("<unknown: host-truncated>");
  expect(clean.resource.attributes.note).toBe("<unknown: host-truncated>");
  expect((clean.instrumentationScope as typeof clean.instrumentationScope & { attributes?: Attributes }).attributes?.note).toBe("<unknown: host-truncated>");
  expect(clean.events[0].attributes?.["exception.message"]).toBe("<unknown: host-truncated>");
  expect(clean.links[0].attributes?.note).toBe("<unknown: host-truncated>");
  expect(clean.attributes["gen_ai.usage.total_tokens"]).toBe(7);
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.events[0].name).toBe("<unknown: host-truncated>");
  expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: { note: "<unknown: host-truncated>" } });
  expect(clean.attributes).toEqual({ note: "<unknown: host-truncated>", "gen_ai.usage.total_tokens": 7, "traceai.capture.host_data_unknown": true });
  expect(clean.status).toEqual({ code: 2, message: "<unknown: host-truncated>" });
  expect(clean.resource.attributes).toEqual({ note: "<unknown: host-truncated>" });
  expect(clean.events).toHaveLength(1); expect(clean.links).toHaveLength(1);
  expect(clean.events[0].attributes).toEqual({ "exception.message": "<unknown: host-truncated>" });
  expect(clean.links[0].attributes).toEqual({ note: "<unknown: host-truncated>" });
  expect(clean.spanContext()).toEqual(native.spanContext());
  expect(JSON.stringify(clean)).not.toContain("cut/v1:");
  expect(JSON.stringify(mapped)).toBe(before);
});

test("R2 known-before-write status is protected natively in auto and standalone", async () => {
  registerSecret("known-r2-before-write-credential");
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    for (const auto of [true, false]) {
      const fi = new FITracer({ tracer: (auto ? FITracer.autoProvider(provider) : provider).getTracer("authored") }).startSpan("status");
      fi.setStatus({ code: 2, message: "retry known-r2-before-write-credential now" }); fi.end();
    }
    for (const native of exporter.getFinishedSpans()) {
      expect(native.status.message).toBe("retry <redacted> now");
      expect(privacyFilter().span(native).status.message).toBe("retry <redacted> now");
      expect(native.status.message).toBe("retry <redacted> now");
    }
  } finally { await provider.shutdown(); }
});

test("R2 late status secrets are rechecked only on the detached auto view", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const fi = new FITracer({ tracer: FITracer.autoProvider(provider).getTracer("authored") }).startSpan("late");
    const native: any = fi.getContextSpan();
    native.setAttribute("app.note", "retry late-r2-status-credential now");
    fi.setStatus({ code: 2, message: "retry late-r2-status-credential now" });
    registerSecret("late-r2-status-credential"); fi.end();
    const before = JSON.stringify([native.attributes, native.status]);
    const clean = privacyFilter().span(exporter.getFinishedSpans()[0]);
    expect(native.status.message).toBe("retry late-r2-status-credential now");
    expect(clean.status.message).toBe("retry <redacted> now");
    expect(clean.attributes["app.note"]).toBe("retry <redacted> now");
    expect(JSON.stringify([native.attributes, native.status])).toBe(before);
  } finally { await provider.shutdown(); }
});

test.each(["start-mime", "updates-mime", "updates-type"])("R2 authored ancestry survives %s without replaying foreign storage", async schedule => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    for (const auto of [true, false]) {
      const fi = new FITracer({ tracer: (auto ? FITracer.autoProvider(provider) : provider).getTracer("authored") })
        .startSpan("media", { attributes: schedule === "start-mime" ? { mime_type: "audio/pcm" } : {} });
      if (schedule === "updates-mime") fi.setAttribute("mime_type", "audio/pcm");
      if (schedule === "updates-type") fi.setAttributes({ type: "audio" });
      if (auto) fi.getContextSpan().setAttribute("app.note", "foreign prose stays intact");
      fi.setAttribute("gen_ai.audio.bytes", 7);
      fi.setAttributes({ "gen_ai.usage.total_tokens": 11 }); fi.end();
    }
    const [native, standalone] = exporter.getFinishedSpans();
    for (const span of [native, standalone]) {
      expect(span.attributes["gen_ai.audio.bytes"]).toBe("<omitted: media>");
      expect(span.attributes["gen_ai.usage.total_tokens"]).toBe("<omitted: media>");
      expect(privacyFilter().span(span).attributes["gen_ai.audio.bytes"]).toBe("<omitted: media>");
    }
    expect(native.attributes["app.note"]).toBe("foreign prose stays intact");
  } finally { await provider.shutdown(); }
});

test("R2 classification does not harvest foreign MIME or replay a foreign overwrite", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const fi = new FITracer({ tracer: FITracer.autoProvider(provider).getTracer("authored") }).startSpan("foreign");
    const native: any = fi.getContextSpan();
    native.setAttributes({ mime_type: "audio/pcm", "app.note": "foreign untouched" });
    fi.setAttribute("gen_ai.audio.bytes", 7);
    expect(native.attributes["gen_ai.audio.bytes"]).toBe(7);
    native.setAttribute("gen_ai.audio.bytes", 9); fi.end();
    const before = JSON.stringify(native.attributes), clean = privacyFilter().span(exporter.getFinishedSpans()[0]);
    expect(native.attributes["gen_ai.audio.bytes"]).toBe(9);
    expect(clean.attributes["gen_ai.audio.bytes"]).toBe("<omitted: media>");
    expect(JSON.stringify(native.attributes)).toBe(before);
  } finally { await provider.shutdown(); }
});

test("R2 huge nonmatching text is budgeted before any marker regex and fails closed", () => {
  const huge = "ordinary text! ".repeat(400000), native = fixture({ note: huge });
  const originalTest = RegExp.prototype.test;
  let longestScan = 0;
  const spy = jest.spyOn(RegExp.prototype, "test").mockImplementation(function (this: RegExp, value: string) {
    longestScan = Math.max(longestScan, value.length); return originalTest.call(this, value);
  });
  try { rememberSpanValueLimit(native); } finally { spy.mockRestore(); }
  expect(longestScan).toBeLessThanOrEqual(4 * 1024 * 1024);
  const mapped = { ...native, attributes: { note: "small prose", "gen_ai.usage.total_tokens": 7 } };
  const clean = privacyFilter().span(mapped);
  expect(clean.attributes).toEqual({});
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.status).toEqual({ code: 2 });
  expect(native.attributes.note).toBe(huge); expect(mapped.attributes.note).toBe("small prose");
  expect(privacyFilter().span(native).attributes).toEqual({});
});

test("R2 loss character budget is shared across event and link values", () => {
  const chunk = "ordinary! ".repeat(110000), native = fixture({});
  native.events = [0, 1, 2].map(i => ({ name: `event ${i}`, attributes: { note: chunk } }));
  native.links = [0, 1].map(() => ({ context: native.spanContext(), attributes: { note: chunk } }));
  rememberSpanValueLimit(native);
  const copy = { ...native, attributes: { note: "short copy" }, events: [], links: [] };
  const clean = privacyFilter().span(copy);
  expect(clean.attributes).toEqual({});
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.status).toEqual({ code: 2 });
  expect(native.events[0].attributes.note).toBe(chunk); expect(native.links[1].attributes.note).toBe(chunk);
});

test("R2 short marker and at-limit positives still withhold; exact non-content stays useful", () => {
  for (const note of ["cut/v1::", "tiny [truncated]"]) {
    const native = fixture({ note, "gen_ai.operation.name": "text_completion" }); native._attributeValueLengthLimit = 8;
    const clean = privacyFilter().span(native);
    expect(clean.attributes.note).toBe("<unknown: host-truncated>");
    expect(clean.attributes["gen_ai.operation.name"]).toBe("text_completion");
    expect(native.attributes.note).toBe(note);
  }
});

test("R2 withheld registered prefix and session identity keep value-free redaction markers", () => {
  registerSecret("r2-wire-prefix-rest-of-credential");
  const native = fixture({ note: "r2-wire-prefi", "session.id": "ordinary truncated session" }); native._attributeValueLengthLimit = 12;
  const before = JSON.stringify(native), clean = privacyFilter().span(native);
  expect(clean.attributes).toEqual({ note: "<unknown: host-truncated>", "session.id": "<redacted>", "traceai.capture.host_data_unknown": true });
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
  expect(clean.resource.attributes).toEqual({}); expect(clean.status).toEqual({ code: 2 });
  expect(clean.events).toEqual([]); expect(clean.links).toEqual([]);
  expect(clean.spanContext()).toEqual(native.spanContext());
  expect(clean.attributes["session.id"]).toBe("<redacted>");
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  expect(JSON.stringify(native)).toBe(before);
});

test("R2 image URL aliases in errors are hidden without dropping plain error prose", () => {
  const url = "https://example.invalid/private-image";
  const native = fixture({ "image.url": url, "exception.message": `failed ${url} now` });
  native.status.message = `failed ${url} now`;
  native.events = [{ name: "exception", attributes: { "exception.message": `failed ${url} now` } }];
  const before = JSON.stringify(native);
  for (const mode of ["explicit", "env"]) {
    if (mode === "env") process.env.FI_HIDE_INPUT_IMAGES = "true";
    const clean = privacyFilter(mode === "explicit" ? { hideInputImages: true } : {}).span(native);
    expect(clean.status.message).toBe("failed <redacted> now");
    expect(clean.events[0].attributes?.["exception.message"]).toBe("failed <redacted> now");
    expect(JSON.stringify(clean)).not.toContain(url);
  }
  expect(JSON.stringify(native)).toBe(before);
});
