import { context, ROOT_CONTEXT, trace, type Attributes } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { FITracer } from "../trace/trace-config/FITracer";
import { privacyFilter, rememberSpanValueLimit } from "../export-privacy";
import { registerSecret } from "../secrets";

// Authored before candidate production edits. RED is predicted, not executed.
const saved = { ...process.env };
beforeEach(() => { for (const key of Object.keys(process.env)) if (/^(FI_|OTEL_)/.test(key)) delete process.env[key]; });
afterEach(() => { process.env = { ...saved }; context.disable(); trace.disable(); });
function fixture(attributes: Attributes = {}): any {
  const ctx = { traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 };
  return { name: "native", kind: 2, _spanContext: ctx, spanContext: () => ctx, attributes,
    startTime: [1, 0], endTime: [2, 0], duration: [1, 0], ended: true,
    status: { code: 2 }, resource: { attributes: {} }, instrumentationScope: { name: "test" }, events: [], links: [] };
}

test("C3 host-truncated registered-secret prefix is withheld, with a populated native control", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanLimits: { attributeValueLengthLimit: 8 }, spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const native = provider.getTracer("host").startSpan("native", { attributes: { note: "prefix42-rest-of-credential" } });
    native.end();
    registerSecret("prefix42-rest-of-credential");
    const ended = exporter.getFinishedSpans()[0];
    expect(ended.attributes.note).toBe("prefix42");
    const clean = privacyFilter().span(ended);
    expect(clean.attributes).toEqual({ note: "<unknown: host-truncated>", "traceai.capture.host_data_unknown": true });
    expect(clean.name).toBe("<unknown: host-truncated>");
    expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
    for (const value of Object.values(clean.resource.attributes)) expect(value).toBe("<unknown: host-truncated>");
    expect(clean.status).toEqual({ code: ended.status.code });
    expect(clean.events).toEqual([]); expect(clean.links).toEqual([]);
    expect(clean.spanContext()).toMatchObject({ traceId: ended.spanContext().traceId, spanId: ended.spanContext().spanId });
    expect(clean.droppedAttributesCount).toBe(ended.droppedAttributesCount);
    expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
    expect(JSON.stringify(clean)).not.toContain("prefix42");
    expect(ended.attributes.note).toBe("prefix42");
  } finally { await provider.shutdown(); }
});

test.each(["span", "event", "link", "exception", "status", "resource", "scope"])("C3 all-surface truncation withholding: %s", surface => {
  registerSecret("prefix42-rest-of-credential");
  const raw = fixture(); raw._attributeValueLengthLimit = 8;
  if (surface === "span") raw.attributes.note = "prefix42";
  if (surface === "event" || surface === "exception") raw.events = [{ name: surface === "exception" ? "exception" : "event", time: [1, 0], attributes: { "exception.message": "prefix42" } }];
  if (surface === "link") raw.links = [{ context: raw.spanContext(), attributes: { note: "prefix42" } }];
  if (surface === "status") raw.status.message = "prefix42";
  if (surface === "resource") raw.resource.attributes.note = "prefix42";
  if (surface === "scope") raw.instrumentationScope.attributes = { note: "prefix42" };
  const before = JSON.stringify(raw);
  const clean = privacyFilter().span(raw);
  expect(before).toContain("prefix42");
  expect(JSON.stringify(clean)).not.toContain("prefix42");
  expect(clean.attributes).toEqual({ ...(surface === "span" ? { note: "<unknown: host-truncated>" } : {}), "traceai.capture.host_data_unknown": true });
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.status).toEqual({ code: 2, ...(surface === "status" ? { message: "<unknown: host-truncated>" } : {}) });
  expect(clean.resource.attributes).toEqual(surface === "resource" ? { note: "<unknown: host-truncated>" } : {});
  expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: surface === "scope" ? { note: "<unknown: host-truncated>" } : {} });
  expect(clean.events).toHaveLength(surface === "event" || surface === "exception" ? 1 : 0);
  if (surface === "event" || surface === "exception") { expect(clean.events[0].name).toBe("<unknown: host-truncated>"); expect(clean.events[0].attributes).toEqual({ "exception.message": "<unknown: host-truncated>" }); }
  expect(clean.links).toHaveLength(surface === "link" ? 1 : 0);
  if (surface === "link") expect(clean.links[0].attributes).toEqual({ note: "<unknown: host-truncated>" });
  expect(clean.spanContext()).toEqual(raw.spanContext());
  expect(JSON.stringify(raw)).toBe(before);
});

test("C3 arrays are withheld as a whole and explicit markers cannot carry a prefix", () => {
  registerSecret("prefix42-rest-of-credential");
  const raw = fixture({ list: ["short", "prefix42"], marked: "tiny [truncated]", safe: "ok!" });
  raw._attributeValueLengthLimit = 8;
  expect(privacyFilter().span(raw).attributes).toEqual({
    list: "<unknown: host-truncated>", marked: "<unknown: host-truncated>", safe: "<unknown: host-truncated>", "traceai.capture.host_data_unknown": true,
  });
});

test("C3 non-content exemption is exact, never a usage prefix exemption or a secret exemption", () => {
  registerSecret("31415926");
  registerSecret("prefix42-rest-of-credential");
  const raw = fixture({ "gen_ai.operation.name": "text_completion", "gen_ai.usage.user_payload": "prefix42", "gen_ai.usage.total_tokens": 31415926 });
  raw._attributeValueLengthLimit = 8;
  const clean = privacyFilter().span(raw);
  expect(clean.attributes["gen_ai.operation.name"]).toBe("text_completion");
  expect(clean.attributes).toEqual({ "gen_ai.operation.name": "text_completion", "gen_ai.usage.user_payload": "<unknown: host-truncated>", "gen_ai.usage.total_tokens": "<redacted>", "traceai.capture.host_data_unknown": true });
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
  expect(clean.resource.attributes).toEqual({}); expect(clean.status).toEqual({ code: 2 });
  expect(clean.events).toEqual([]); expect(clean.links).toEqual([]);
  expect(clean.spanContext()).toEqual(raw.spanContext());
  expect(clean.attributes["gen_ai.usage.total_tokens"]).toBe("<redacted>");
});

test("C3 shortened mapped fragments stay unknown after native loss observation", () => {
  const native = fixture({ note: "prefix42" }); native._attributeValueLengthLimit = 8;
  rememberSpanValueLimit(native);
  const mapped = { ...native, attributes: { note: "prefix" } };
  delete mapped._attributeValueLengthLimit;
  expect(privacyFilter().span(mapped).attributes.note).toBe("<unknown: host-truncated>");
  expect(mapped.attributes.note).toBe("prefix");
});

test("C3 dropped events and links mark unknown without inventing records", () => {
  const raw = fixture({ safe: "ordinary words" }); raw._droppedEventsCount = 2; raw._droppedLinksCount = 1;
  const clean = privacyFilter().span(raw);
  expect(clean.attributes["traceai.capture.host_data_unknown"]).toBe(true);
  expect(clean.droppedEventsCount).toBe(2); expect(clean.droppedLinksCount).toBe(1);
  expect(clean.events).toEqual([]); expect(clean.links).toEqual([]);
  expect(clean.attributes).toEqual({ safe: "<unknown: host-truncated>", "traceai.capture.host_data_unknown": true });
  expect(clean.name).toBe("<unknown: host-truncated>");
  expect(clean.instrumentationScope).toEqual({ name: "<unknown: host-truncated>", version: "<unknown: host-truncated>", schemaUrl: "<unknown: host-truncated>", attributes: {} });
  expect(clean.resource.attributes).toEqual({}); expect(clean.status).toEqual({ code: 2 });
  expect(clean.spanContext()).toEqual(raw.spanContext());
});

test("C3 foreign writes remain byte-identical while FI-authored hidden attributes stay hidden", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const tracer = new FITracer({ tracer: FITracer.autoProvider(provider).getTracer("auto"), traceConfig: { hideInputs: true, hideOutputs: true } });
    const fi = tracer.startSpan("native", { attributes: { "input.value": "private authored input" } });
    const native: any = fi.getContextSpan();
    expect(native).not.toBe(fi);
    native.setAttributes({ "app.note": "untouched foreign text", "app.list": ["one", "two"] });
    const foreignBefore = JSON.stringify({ note: native.attributes["app.note"], list: native.attributes["app.list"] });
    fi.setAttributes({ "output.value": "private authored output", "gen_ai.usage.input_tokens.audio": undefined });
    fi.end();
    const ended = exporter.getFinishedSpans()[0];
    const clean = privacyFilter({ hideInputs: true, hideOutputs: true }).span(ended);
    expect(JSON.stringify({ note: ended.attributes["app.note"], list: ended.attributes["app.list"] })).toBe(foreignBefore);
    expect(ended.attributes["input.value"]).toBe("__REDACTED__");
    expect(ended.attributes["output.value"]).toBe("__REDACTED__");
    expect(ended.attributes).not.toHaveProperty("gen_ai.usage.input_tokens.audio");
    expect(clean.attributes["input.value"]).toBe("__REDACTED__");
    expect(clean.attributes["output.value"]).toBe("__REDACTED__");
    expect(clean).not.toBe(ended); expect(clean.attributes).not.toBe(ended.attributes);
    (clean.attributes["app.list"] as string[])[0] = "changed detached copy";
    expect(ended.attributes["app.list"]).toEqual(["one", "two"]);
  } finally { await provider.shutdown(); }
});

test("D6 auto active context is native; explicit standalone active context remains the FI wrapper", async () => {
  context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    for (const auto of [true, false]) {
      const tracer = new FITracer({ tracer: (auto ? FITracer.autoProvider(provider) : provider).getTracer("mode"), traceConfig: { hideInputs: true } });
      tracer.startActiveSpan("native", {}, ROOT_CONTEXT, span => {
        const active = trace.getActiveSpan()!;
        expect(active === span).toBe(!auto);
        active.setAttribute("input.value", "application input");
        span.end();
      });
    }
    expect(exporter.getFinishedSpans().map(s => s.attributes["input.value"])).toEqual(["application input", "__REDACTED__"]);
  } finally { await provider.shutdown(); }
});

test("destination status echo is native only, with a late-secret recheck on every FI surface", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const fi = new FITracer({ tracer: FITracer.autoProvider(provider).getTracer("auto") }).startSpan("native");
    const native: any = fi.getContextSpan();
    const message = "retry late-interim-secret now";
    native.setAttribute("note", message);
    native.addEvent("event", { note: message }); native.addLink({ context: native.spanContext(), attributes: { note: message } });
    fi.setStatus({ code: 2, message }); fi.end();
    registerSecret("late-interim-secret");
    const ended = exporter.getFinishedSpans()[0]; const before = JSON.stringify({ attributes: ended.attributes, status: ended.status, events: ended.events, links: ended.links });
    const clean = privacyFilter().span(ended);
    expect(ended.status.message).toBe("retry late-interim-secret now");
    expect(clean.status.message).toBe("retry <redacted> now");
    expect(clean.attributes.note).toBe("retry <redacted> now");
    expect(clean.events[0].attributes?.note).toBe("retry <redacted> now");
    expect(clean.links[0].attributes?.note).toBe("retry <redacted> now");
    expect(JSON.stringify({ attributes: ended.attributes, status: ended.status, events: ended.events, links: ended.links })).toBe(before);
  } finally { await provider.shutdown(); }
});

test.each(["explicit", "environment", "environment-empty-error"])("B2/A1 image policy retains plain errors without an error-hide implication: %s", async mode => {
  if (mode !== "explicit") process.env.FI_HIDE_INPUT_IMAGES = "true";
  if (mode === "environment-empty-error") process.env.FI_HIDE_ERROR_DETAILS = "";
  // PENDING Z-OVER: F2 cap-0 whole-scope replacement precedes I1 media markers.
  for (const base64ImageMaxLength of [undefined, 32000]) {
  const options = { hideErrorDetails: false, ...(mode === "explicit" ? { hideInputImages: true } : {}), ...(base64ImageMaxLength === undefined ? {} : { base64ImageMaxLength }) };
  const exporter = new InMemorySpanExporter(); const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  try {
    const tracer = new FITracer({ tracer: FITracer.autoProvider(provider).getTracer("auto"), traceConfig: options });
    for (const message of ["ordinary error text", "Cannot decode data:image/png;base64,iVBORw0KGgoAAAANSUhEUg== retry"]) {
      const span = tracer.startSpan("native"); const error = new Error(message);
      span.recordException(error); span.setStatus({ code: 2, message }); span.end();
      expect(error.message).toBe(message);
    }
    for (const native of exporter.getFinishedSpans()) {
      for (const value of [native, privacyFilter(options).span(native)]) {
        expect(value.events.filter(e => e.name === "exception")).toHaveLength(1);
        expect(JSON.stringify(value.events)).not.toContain("data:image");
        expect(JSON.stringify(value.events)).not.toContain("iVBORw0K");
        if (base64ImageMaxLength === undefined && native !== exporter.getFinishedSpans()[0]) {
          expect(value.status.message).toBe("[image-redacted]");
        } else {
        expect(value.status.message).toBe(native === exporter.getFinishedSpans()[0] ? "ordinary error text" : "Cannot decode <omitted: 24 base64 chars> retry");
        }
      }
    }
  } finally { await provider.shutdown(); }
  }
});

test("A7 absence survives admission and export before coercion; populated literal undefined is distinct", () => {
  const input = { "gen_ai.usage.input_tokens.audio": undefined, "gen_ai.usage.input_tokens": 3, literal: "undefined" };
  expect(privacyFilter().admissionAttributes(input)).toEqual({ "gen_ai.usage.input_tokens": 3, literal: "undefined" });
  expect(privacyFilter().span(fixture(input)).attributes).toEqual({ "gen_ai.usage.input_tokens": 3, literal: "undefined" });
});

test("COMPOSED_SECRET_PRECEDENCE exact registered numeric secrets beat media omission", () => {
  registerSecret("31415926");
  for (const filter of [privacyFilter(), privacyFilter({ hideInputImages: true })]) {
    const clean = filter.span(fixture({ audio: 31415926, "gen_ai.audio.bytes": 31415926, "gen_ai.usage.total_tokens": 31415926, safe: 7 }));
    expect(clean.attributes).toEqual({ audio: "<redacted>", "gen_ai.audio.bytes": "<redacted>", "gen_ai.usage.total_tokens": "<redacted>", safe: 7 });
  }
});
